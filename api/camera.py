"""Home camera helpers: stream URL validation (no embedded credentials, private network by default), a dependency-free
RTSP OPTIONS/DESCRIBE probe used by "Test connection", worker keys, and the motion/activity analyser the local worker
runs on frames. Nothing here produces medical conclusions; outputs are movement/presence metadata with confidences.
"""
import base64
import hashlib
import ipaddress
import json
import secrets
import socket
import time
from urllib.parse import urlsplit, urlunsplit

PRIVATE_OK=('rtsp','rtsps','http','https','onvif')

def split_credentials(url):
    """Return (clean_url, username, password). Credentials embedded in the URL are moved out and never stored inline."""
    parts=urlsplit(url.strip())
    if parts.scheme.lower() not in PRIVATE_OK:raise ValueError('Use an rtsp://, rtsps://, http:// or https:// stream URL')
    host=parts.hostname
    if not host:raise ValueError('Stream URL needs a host')
    user=parts.username or '';pw=parts.password or ''
    netloc=host+(f':{parts.port}' if parts.port else '')
    clean=urlunsplit((parts.scheme.lower(),netloc,parts.path or '',parts.query,''))
    return clean,user,pw

def is_private_host(host):
    try:
        ip=ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return host.endswith(('.local','.lan','.home')) or host=='localhost'

def validate_stream(url,local_only=True):
    clean,user,pw=split_credentials(url)
    host=urlsplit(clean).hostname
    if local_only and not is_private_host(host):raise ValueError(f'{host} is not a private-network address. Keep the camera on your home network or explicitly allow a remote address.')
    return clean,user,pw

def redact(url):
    """Display form of a URL without any credentials or query secrets."""
    try:
        p=urlsplit(url);return urlunsplit((p.scheme,(p.hostname or '')+(f':{p.port}' if p.port else ''),p.path,'',''))
    except Exception:return '<invalid url>'

def new_worker_key():
    key=secrets.token_urlsafe(32);return key,hashlib.sha256(key.encode()).hexdigest()

def key_hash(key):return hashlib.sha256(key.encode()).hexdigest()

# ------------------------------------------------------------------ RTSP probe (text protocol, no third-party library)
def rtsp_probe(url,username='',password='',timeout=4.0):
    """OPTIONS then DESCRIBE over a short TCP session. Returns a dict with ok/status/detail; never raises for network errors."""
    p=urlsplit(url);host=p.hostname;port=p.port or 554
    try:
        t0=time.time()
        with socket.create_connection((host,port),timeout=timeout) as s:
            s.settimeout(timeout)
            def send(method,cseq,extra=''):
                req=f'{method} {url} RTSP/1.0\r\nCSeq: {cseq}\r\nUser-Agent: BabyCare-Camera-Worker\r\n{extra}\r\n'
                s.sendall(req.encode());data=b''
                while b'\r\n\r\n' not in data and len(data)<65536:
                    chunk=s.recv(4096)
                    if not chunk:break
                    data+=chunk
                head=data.decode('latin-1',errors='replace');status=int(head.split(' ',2)[1]) if head.startswith('RTSP/') and len(head.split(' '))>1 else 0
                return status,head
            status,head=send('OPTIONS',1)
            if status==0:return {'ok':False,'status':0,'detail':'No RTSP reply (is this an RTSP stream?)','latency_ms':int((time.time()-t0)*1000)}
            auth=''
            if username:auth='Authorization: Basic '+base64.b64encode(f'{username}:{password}'.encode()).decode()+'\r\n'
            status,head=send('DESCRIBE',2,auth+'Accept: application/sdp\r\n')
            if status==401 and 'Digest' in head and username:
                import re
                realm=re.search(r'realm="([^"]*)"',head);nonce=re.search(r'nonce="([^"]*)"',head)
                if realm and nonce:
                    ha1=hashlib.md5(f'{username}:{realm[1]}:{password}'.encode()).hexdigest();ha2=hashlib.md5(f'DESCRIBE:{url}'.encode()).hexdigest();resp=hashlib.md5(f'{ha1}:{nonce[1]}:{ha2}'.encode()).hexdigest()
                    status,head=send('DESCRIBE',3,f'Authorization: Digest username="{username}", realm="{realm[1]}", nonce="{nonce[1]}", uri="{url}", response="{resp}"\r\nAccept: application/sdp\r\n')
            detail={200:'Stream described successfully',401:'Camera rejected the username/password',404:'Stream path not found on the camera',403:'Access forbidden by the camera',454:'Session not found',461:'Unsupported transport'}.get(status,f'RTSP status {status}')
            video='m=video' in head
            return {'ok':status==200,'status':status,'detail':detail+(' (video track present)' if video else ''),'latency_ms':int((time.time()-t0)*1000)}
    except socket.timeout:return {'ok':False,'status':0,'detail':'Timed out connecting to the camera','latency_ms':int(timeout*1000)}
    except OSError as exc:return {'ok':False,'status':0,'detail':f'Connection failed: {exc.__class__.__name__}','latency_ms':0}

def http_probe(url,username='',password='',timeout=4.0):
    import httpx
    try:
        auth=(username,password) if username else None
        r=httpx.head(url,auth=auth,timeout=timeout,follow_redirects=False)
        return {'ok':r.status_code<400,'status':r.status_code,'detail':'Reachable' if r.status_code<400 else f'HTTP {r.status_code}','latency_ms':int(r.elapsed.total_seconds()*1000)}
    except Exception as exc:return {'ok':False,'status':0,'detail':f'Connection failed: {exc.__class__.__name__}','latency_ms':0}

def probe(protocol,url,username='',password=''):
    return rtsp_probe(url,username,password) if url.lower().startswith('rtsp') else http_probe(url,username,password)

# ------------------------------------------------------------------ frame analyser (works on any sequence of grayscale frames)
class MotionAnalyser:
    """Turns successive downscaled grayscale frames into coarse activity segments.

    Segments: 'movement' (motion ratio above the active threshold), 'low_activity' (little change for a sustained
    period), 'presence'/'no_presence' (foreground against a slowly adapting background). Posture or gait classes are
    NOT produced by this analyser; 'walking_like' / 'crawling_like' require a pose model that this build does not ship,
    so they are never emitted here. Confidence is a bounded function of signal strength and segment length.
    """
    def __init__(self,active_threshold=0.025,quiet_threshold=0.004,min_segment_s=20,presence_threshold=0.02):
        self.active=active_threshold;self.quiet=quiet_threshold;self.min_segment=min_segment_s;self.presence=presence_threshold
        self.prev=None;self.background=None;self.state=None;self.state_since=None;self.ratios=[];self.events=[]
    @staticmethod
    def _ratio(a,b):
        n=len(a);diff=0
        for i in range(n):
            if abs(a[i]-b[i])>25:diff+=1
        return diff/max(n,1)
    def feed(self,frame,ts):
        """frame: flat list/bytes of grayscale 0..255 values (small, e.g. 64x36). ts: epoch seconds."""
        frame=list(frame)
        if self.background is None:self.background=frame[:]
        else:
            for i,v in enumerate(frame):self.background[i]=int(self.background[i]*0.97+v*0.03)
        motion=self._ratio(frame,self.prev) if self.prev is not None else 0.0
        presence=self._ratio(frame,self.background)
        self.prev=frame;self.ratios.append(motion)
        kind='movement' if motion>=self.active else 'low_activity' if motion<=self.quiet else self.state or 'low_activity'
        if kind!=self.state:
            self._close(ts);self.state=kind;self.state_since=ts;self.ratios=[motion]
        return {'motion_ratio':round(motion,4),'presence_ratio':round(presence,4),'state':self.state}
    def _close(self,ts):
        if self.state and self.state_since is not None and ts-self.state_since>=self.min_segment:
            avg=sum(self.ratios)/max(len(self.ratios),1);length=ts-self.state_since
            strength=min(1.0,avg/(self.active*2)) if self.state=='movement' else min(1.0,(self.quiet*2-avg)/(self.quiet*2)) if self.state=='low_activity' else 0.5
            confidence=round(0.4+0.4*max(0.0,strength)+0.2*min(1.0,length/300),2)
            self.events.append({'kind':self.state,'started_at':self.state_since,'ended_at':ts,'confidence':confidence,'metrics':{'avg_motion_ratio':round(avg,4),'frames':len(self.ratios)}})
    def flush(self,ts):
        self._close(ts);self.state=None;self.state_since=None;out=self.events;self.events=[];return out

def dedupe_key(camera_id,kind,started_at):return f'{camera_id}:{kind}:{int(started_at)}'
