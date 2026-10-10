"""Home Activity Camera tests: credential protection, stream validation, RTSP failure handling, worker ingest and
disconnect/reconnect, parent review states, journal suggestions, and the safety boundary (no medical inference)."""
import json
import socket
import threading
import uuid
from datetime import datetime, timedelta, timezone
import httpx
import pytest
from tests.test_api import api, make_user, make_child, client
from api.database import SessionLocal
from api.models import RateWindow
from api import camera as CAM
from api.camera_worker import Worker

@pytest.fixture(autouse=True)
def clear_rate_window():
    with SessionLocal() as db:db.query(RateWindow).delete();db.commit()

def family():
    t=make_user('cam'+uuid.uuid4().hex[:10]);p=make_child(t);return t,p

def fake_rtsp_server(auth_required=False):
    srv=socket.socket();srv.bind(('127.0.0.1',0));srv.listen(2);port=srv.getsockname()[1]
    def serve():
        for _ in range(3):
            try:conn,_=srv.accept()
            except OSError:return
            with conn:
                conn.settimeout(3)
                for _ in range(4):
                    try:data=conn.recv(4096)
                    except OSError:break
                    if not data:break
                    text=data.decode('latin-1');cseq=[l for l in text.split('\r\n') if l.startswith('CSeq')][0]
                    if text.startswith('OPTIONS'):conn.sendall(f'RTSP/1.0 200 OK\r\n{cseq}\r\nPublic: OPTIONS, DESCRIBE\r\n\r\n'.encode())
                    elif text.startswith('DESCRIBE'):
                        if auth_required and 'Authorization: Basic' not in text:conn.sendall(f'RTSP/1.0 401 Unauthorized\r\n{cseq}\r\nWWW-Authenticate: Basic realm="cam"\r\n\r\n'.encode())
                        else:sdp='v=0\r\nm=video 0 RTP/AVP 96\r\n';conn.sendall(f'RTSP/1.0 200 OK\r\n{cseq}\r\nContent-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}'.encode())
    threading.Thread(target=serve,daemon=True).start();return srv,port

def test_stream_validation_strips_credentials_and_enforces_private_network():
    clean,u,pw=CAM.validate_stream('rtsp://admin:secret@192.168.1.20:554/stream1',True)
    assert clean=='rtsp://192.168.1.20:554/stream1' and u=='admin' and pw=='secret'
    with pytest.raises(ValueError):CAM.validate_stream('rtsp://8.8.8.8/stream',True)
    assert CAM.validate_stream('rtsp://8.8.8.8/stream',False)[0]=='rtsp://8.8.8.8/stream'
    with pytest.raises(ValueError):CAM.validate_stream('ftp://192.168.1.2/x',True)
    assert 'secret' not in CAM.redact('rtsp://admin:secret@192.168.1.20/stream1?token=abc')

def test_rtsp_probe_success_auth_and_failure_handling():
    srv,port=fake_rtsp_server()
    ok=CAM.rtsp_probe(f'rtsp://127.0.0.1:{port}/stream');assert ok['ok'] and ok['status']==200 and 'video track' in ok['detail']
    srv.close()
    srv2,port2=fake_rtsp_server(auth_required=True)
    denied=CAM.rtsp_probe(f'rtsp://127.0.0.1:{port2}/stream');assert not denied['ok'] and denied['status']==401
    allowed=CAM.rtsp_probe(f'rtsp://127.0.0.1:{port2}/stream','user','pw');assert allowed['ok']
    srv2.close()
    closed=socket.socket();closed.bind(('127.0.0.1',0));dead=closed.getsockname()[1];closed.close()
    fail=CAM.rtsp_probe(f'rtsp://127.0.0.1:{dead}/stream',timeout=1);assert not fail['ok'] and 'failed' in fail['detail'].casefold() or 'Timed out' in fail['detail']

def test_motion_analyser_emits_only_movement_classes():
    an=CAM.MotionAnalyser(min_segment_s=5)
    base=[70]*(64*36)
    import random;random.seed(1)
    t=1000.0
    for i in range(40):
        frame=[min(255,max(0,v+(random.randint(-80,80) if i<20 and random.random()<0.4 else 0))) for v in base]
        an.feed(frame,t+i)
    events=an.flush(t+40)
    kinds={e['kind'] for e in events};assert kinds and kinds<={'movement','low_activity'} and all(0.4<=e['confidence']<=1 for e in events)
    assert not any(k in ('fever','illness','walking_like','crawling_like') for k in kinds)

def test_camera_api_hides_credentials_and_handles_review_states():
    t,p=family();other=make_user('cam'+uuid.uuid4().hex[:10])
    bad=api('POST',f'/api/patients/{p}/cameras',t,json={'name':'Nursery','stream_url':'rtsp://admin:pw@93.184.216.34/live','local_network_only':True})
    assert bad.status_code==422
    cam=api('POST',f'/api/patients/{p}/cameras',t,json={'name':'Nursery','room':'Nursery','stream_url':'rtsp://admin:pw@192.168.0.50:554/live','enabled':True,'analysis_mode':'motion'})
    assert cam.status_code==201,cam.text;c=cam.json();cid=c['id']
    dump=json.dumps(c);assert 'pw' not in dump.split('"stream_display"')[1][:60] and c['stream_display']=='rtsp://192.168.0.50:554/live' and c['has_credentials'] and c['audio_enabled'] is False and c['record_clips'] is False and c['local_network_only']
    assert 'password' not in c and 'username' not in c and 'stream_url' not in c
    assert api('GET',f'/api/patients/{p}/cameras',other).status_code==404 and api('POST',f'/api/cameras/{cid}/test',other).status_code==404
    key=api('POST',f'/api/cameras/{cid}/worker-key',t).json()['worker_key']
    assert api('GET',f'/api/cameras/{cid}/worker-config',headers={'X-Camera-Worker-Key':'wrong'}).status_code==401
    cfg=api('GET',f'/api/cameras/{cid}/worker-config',headers={'X-Camera-Worker-Key':key}).json();assert cfg['username']=='admin' and cfg['password']=='pw' and cfg['enabled']
    now=datetime.now(timezone.utc)
    ev=lambda kind,offset,conf=0.8:{'kind':kind,'started_at':(now-timedelta(minutes=offset)).isoformat(),'ended_at':(now-timedelta(minutes=offset-5)).isoformat(),'confidence':conf,'metrics':{'avg_motion_ratio':0.1},'dedupe_key':f'{kind}-{offset}'}
    r=api('POST',f'/api/cameras/{cid}/worker/ingest',headers={'X-Camera-Worker-Key':key},json={'worker_id':'w1','state':'connected','events':[ev('movement',60),ev('low_activity',40),ev('walking_like',30,0.4),ev('movement',60)]})
    assert r.status_code==200 and r.json()=={'accepted':2,'skipped':2,'enabled':True,'analysis_mode':'motion','record_clips':False,'audio_enabled':False}
    assert api('POST',f'/api/cameras/{cid}/worker/ingest',headers={'X-Camera-Worker-Key':key},json={'worker_id':'w1','events':[{'kind':'fever','started_at':now.isoformat(),'confidence':0.9,'dedupe_key':'x1'}]}).status_code==422
    obs=api('GET',f'/api/patients/{p}/camera-observations',t).json()['items'];assert len(obs)==2 and all(o['label']=='AI OBSERVED' for o in obs) and obs[0]['confidence_pct']==80
    an=api('GET',f'/api/patients/{p}/camera-analytics',t).json();assert an['confirmed_events']==0 and an['pending_ai_events']==2
    mv=next(o for o in obs if o['kind']=='movement');lo=next(o for o in obs if o['kind']=='low_activity')
    assert api('POST',f'/api/camera-observations/{mv["id"]}/review',t,json={'action':'confirm'}).json()['label']=='PARENT CONFIRMED'
    corrected=api('POST',f'/api/camera-observations/{lo["id"]}/review',t,json={'action':'correct','corrected_kind':'rest','note':'he was asleep'}).json();assert corrected['label']=='PARENT CORRECTED' and corrected['effective_kind']=='rest'
    an=api('GET',f'/api/patients/{p}/camera-analytics',t).json();assert an['confirmed_events']==2 and an['pending_ai_events']==0 and an['kinds']['rest']==1
    added=api('POST',f'/api/camera-observations/{mv["id"]}/add-to-journal',t,json={'as_kind':'activity','activity_kind':'playing'});assert added.status_code==201,added.text
    entry=added.json()['entry'];assert entry['source']=='camera_confirmed' and entry['duration_minutes']==5
    assert api('POST',f'/api/camera-observations/{mv["id"]}/add-to-journal',t,json={}).status_code==409
    tl=api('GET',f'/api/patients/{p}/care/timeline?filters=activity,camera',t).json()['items'];assert any(x['kind']=='activity' and x['source']=='camera_confirmed' for x in tl)
    assert api('POST',f'/api/cameras/{cid}/disable',t).json()['worker_state']=='disabled'
    r=api('POST',f'/api/cameras/{cid}/worker/ingest',headers={'X-Camera-Worker-Key':key},json={'worker_id':'w1','events':[ev('movement',10)]}).json();assert r['accepted']==0 and r['enabled'] is False
    assert api('GET',f'/api/patients/{p}/care/day',t).json()['symptoms']==[]  # the camera can never create a symptom or fever record
    assert api('DELETE',f'/api/cameras/{cid}',other).status_code==404 and api('DELETE',f'/api/cameras/{cid}',t).json()['ok']

def test_worker_simulation_reconnect_and_ingest_through_api():
    t,p=family()
    cid=api('POST',f'/api/patients/{p}/cameras',t,json={'name':'Play room','stream_url':'rtsp://10.0.0.9/live','enabled':True}).json()['id']
    key=api('POST',f'/api/cameras/{cid}/worker-key',t).json()['worker_key']
    def handler(request):
        r=client.request(request.method,request.url.path,content=request.content,headers={k:v for k,v in request.headers.items() if k.lower() in ('x-camera-worker-key','content-type')})
        return httpx.Response(r.status_code,content=r.content,headers={'content-type':r.headers.get('content-type','application/json')})
    w=Worker('http://testserver',cid,key,worker_id='sim',simulate=120,transport=httpx.MockTransport(handler))
    result=w.run_forever();assert result['status']=='ok' and result['frames']==120 and result['events']>=2
    cams=api('GET',f'/api/patients/{p}/cameras',t).json()['cameras'][0];assert cams['worker_state']=='connected' and cams['worker_seen_at']
    obs=api('GET',f'/api/patients/{p}/camera-observations',t).json()['items'];assert {o['kind'] for o in obs}<={'movement','low_activity'} and all(o['review_status']=='ai_observed' for o in obs)
    # reconnect path: a worker whose source fails reports 'reconnecting' and keeps going until stopped
    class Failing(Worker):
        def run_once(self,max_frames=None):raise ConnectionError('stream dropped')
    f=Failing('http://testserver',cid,key,worker_id='sim2',transport=httpx.MockTransport(handler))
    import time;orig=time.sleep;time.sleep=lambda s:None
    try:f.run_forever(max_cycles=2)
    finally:time.sleep=orig
    assert api('GET',f'/api/patients/{p}/cameras',t).json()['cameras'][0]['worker_state']=='reconnecting'
    wrong=Worker('http://testserver',cid,'not-the-key',simulate=10,transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):wrong.run_once()
