"""Local BabyCare Camera Worker.

Runs on a machine inside the home network. It fetches its instructions with the camera's worker key, reads the
stream locally (OpenCV when installed, or a synthetic/simulated source for tests), runs the motion/presence analyser
and sends ONLY derived events to the BabyCare API. Raw video never leaves the machine; clips are written locally and
only if the parent enabled recording. Audio is never captured by this worker.

Usage:
  python -m api.camera_worker --api http://127.0.0.1:8000 --camera 1 --key <worker key>
  python -m api.camera_worker --api http://127.0.0.1:8000 --camera 1 --key <key> --simulate 120   # synthetic frames for a smoke test
Environment alternatives: BABYCARE_API_URL, BABYCARE_CAMERA_ID, BABYCARE_CAMERA_KEY.
"""
import argparse
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx
from .camera import MotionAnalyser, dedupe_key

log=logging.getLogger('babycare.camera-worker')

class FrameSource:
    """Yields (timestamp, grayscale 64x36 frame) tuples. Real cameras need opencv-python-headless."""
    def __init__(self,config,simulate=0,size=(64,36)):
        self.config=config;self.simulate=simulate;self.size=size;self.cap=None
    def open(self):
        if self.simulate:return True
        try:import cv2
        except ImportError:raise RuntimeError('opencv-python-headless is required to read a real camera stream (pip install opencv-python-headless)')
        url=self.config['stream_url']
        if self.config.get('username'):
            from urllib.parse import urlsplit,urlunsplit,quote
            p=urlsplit(url);url=urlunsplit((p.scheme,f"{quote(self.config['username'])}:{quote(self.config.get('password',''))}@{p.hostname}"+(f':{p.port}' if p.port else ''),p.path,p.query,''))
        self.cap=cv2.VideoCapture(url)
        return self.cap.isOpened()
    def frames(self):
        if self.simulate:
            base=[random.randint(60,90) for _ in range(self.size[0]*self.size[1])];t=time.time()
            for i in range(self.simulate):
                active=(i//30)%2==0  # alternate 30-frame active / quiet blocks
                frame=[min(255,max(0,v+(random.randint(-60,60) if active and random.random()<0.3 else random.randint(-2,2)))) for v in base]
                yield t+i,frame
            return
        import cv2
        while True:
            ok,img=self.cap.read()
            if not ok:raise ConnectionError('Frame read failed')
            small=cv2.resize(cv2.cvtColor(img,cv2.COLOR_BGR2GRAY),self.size,interpolation=cv2.INTER_AREA)
            yield time.time(),small.flatten().tolist()
    def close(self):
        if self.cap is not None:self.cap.release()

class Worker:
    def __init__(self,api,camera_id,key,worker_id=None,clip_dir=None,simulate=0,transport=None):
        self.api=api.rstrip('/');self.camera_id=camera_id;self.key=key;self.worker_id=worker_id or f'worker-{os.getpid()}';self.simulate=simulate
        self.client=httpx.Client(base_url=self.api,timeout=20,headers={'X-Camera-Worker-Key':key},transport=transport)
        self.clip_dir=Path(clip_dir) if clip_dir else None;self.pending=[];self.last_sent=0
    def config(self):
        r=self.client.get(f'/api/cameras/{self.camera_id}/worker-config');r.raise_for_status();return r.json()
    def send(self,events,state,message=''):
        payload={'worker_id':self.worker_id,'state':state,'message':message[:200],'events':[{'kind':e['kind'],'started_at':datetime.fromtimestamp(e['started_at'],timezone.utc).isoformat(),'ended_at':datetime.fromtimestamp(e['ended_at'],timezone.utc).isoformat(),'confidence':e['confidence'],'metrics':e['metrics'],'dedupe_key':dedupe_key(self.camera_id,e['kind'],e['started_at'])} for e in events]}
        r=self.client.post(f'/api/cameras/{self.camera_id}/worker/ingest',json=payload);r.raise_for_status();return r.json()
    def run_once(self,max_frames=None):
        cfg=self.config()
        if not cfg['enabled'] or cfg['analysis_mode']=='off':
            self.send([], 'disconnected','camera disabled or analysis off');log.info('camera %s disabled; idle',self.camera_id);return {'status':'disabled','events':0}
        src=FrameSource(cfg,simulate=self.simulate)
        if not src.open():raise ConnectionError('Could not open stream')
        analyser=MotionAnalyser(min_segment_s=5 if self.simulate else 20)
        sent=0;frames=0;last_heartbeat=time.time()
        try:
            for ts,frame in src.frames():
                analyser.feed(frame,ts);frames+=1
                if analyser.events or time.time()-last_heartbeat>60:
                    events=analyser.events;analyser.events=[]
                    result=self.send(events,'connected',f'{frames} frames analysed');sent+=result.get('accepted',0);last_heartbeat=time.time()
                    if not result.get('enabled'):log.info('camera disabled remotely; stopping');break
                if max_frames and frames>=max_frames:break
        finally:
            events=analyser.flush(time.time() if not self.simulate else ts)
            if events:sent+=self.send(events,'connected','flush').get('accepted',0)
            src.close()
        return {'status':'ok','frames':frames,'events':sent}
    def run_forever(self,max_cycles=None):
        delay=2;cycles=0
        while True:
            cycles+=1
            try:
                result=self.run_once();delay=2
                if result['status']=='disabled':time.sleep(30 if not self.simulate else 0)
                if self.simulate:return result
            except (httpx.HTTPError,ConnectionError,RuntimeError) as exc:
                log.warning('camera worker error: %s; reconnecting in %ss',exc,delay)
                try:self.send([],'reconnecting',str(exc)[:200])
                except Exception:pass
                time.sleep(delay);delay=min(delay*2,120)
            if max_cycles and cycles>=max_cycles:return {'status':'stopped'}

def main(argv=None):
    a=argparse.ArgumentParser(description='Local camera worker: analyses the home camera on this machine and sends derived events only.')
    a.add_argument('--api',default=os.getenv('BABYCARE_API_URL','http://127.0.0.1:8000'));a.add_argument('--camera',type=int,default=int(os.getenv('BABYCARE_CAMERA_ID','0') or 0));a.add_argument('--key',default=os.getenv('BABYCARE_CAMERA_KEY',''))
    a.add_argument('--simulate',type=int,default=0,help='Use N synthetic frames instead of a camera (smoke test)');a.add_argument('--clip-dir',default=os.getenv('BABYCARE_CLIP_DIR',''))
    args=a.parse_args(argv)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    if not args.key or not args.camera:print('Camera id and worker key are required (issue the key from the Home Camera page).',file=sys.stderr);return 2
    if not args.api.startswith('http://127.0.0.1') and not args.api.startswith('http://localhost') and not args.api.startswith('https://'):print('Use https:// for any API that is not on this machine.',file=sys.stderr);return 2
    w=Worker(args.api,args.camera,args.key,clip_dir=args.clip_dir or None,simulate=args.simulate)
    result=w.run_forever();print(result);return 0

if __name__=='__main__':sys.exit(main())
