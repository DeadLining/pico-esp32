#!/usr/bin/env python3
"""Internal-only Pico firmware builder HTTP service."""
import hashlib, json, os, re, shutil, subprocess, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
ROOT=Path(os.environ.get('FIRMWARE_SOURCE_DIR','/opt/pico-esp32')).resolve(); OUT=Path(os.environ.get('FIRMWARE_OUTPUT_DIR','/output')).resolve(); JOBS={}; LOCK=threading.Lock(); ACTIVE=set()

def cache_key(body):
  payload={'source_revision':os.environ.get('FIRMWARE_SOURCE_REVISION','unknown'),'board':body['board'],'name':body['name'],'target':body['target'],'language':body['language'],'wake_word':body['wake_word'],'build_options':body.get('build_options') or {}}
  return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def now_iso():
  return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

def sha256_of(path):
  return hashlib.sha256(path.read_bytes()).hexdigest()

def flash_segments(root):
  """Every flashable segment IDF itself would write, straight from flasher_args.json.

  The browser flasher writes segments individually, so it must never be given the
  merged image: the merged image starts at 0x0 and would overlap every segment.
  """
  args=json.loads((root/'build/flasher_args.json').read_text())
  files=args.get('flash_files') or {}
  if not isinstance(files,dict) or not 1 <= len(files) <= 16:
    raise RuntimeError('invalid IDF flash_files')
  segments=[]
  for offset,rel in files.items():
    src=(root/'build'/rel).resolve()
    if not src.is_file() or not src.is_relative_to((root/'build').resolve()):
      raise RuntimeError('missing or invalid build segment '+str(rel))
    segments.append((int(offset,0),src.name,src))
  segments.sort(key=lambda x:x[0])
  names=[n for _,n,_ in segments]
  if len(set(names)) != len(names): raise RuntimeError('duplicate segment name')
  return segments

def request_of(b):
  return {k:b.get(k) for k in ('board','name','target','language','wake_word')}|{'build_options':b.get('build_options') or {}}

def persist(job):
  out=OUT/job['id']; out.mkdir(parents=True,exist_ok=True)
  (out/'job.json').write_text(json.dumps({k:v for k,v in job.items() if k!='manifest'},ensure_ascii=False,indent=2),encoding='utf-8')

def _manifest_record(out):
  """Rebuild a history record for builds made before job.json existed."""
  manifest=json.loads((out/'manifest.json').read_text())
  if manifest.get('product') != 'Pico': return None
  board=manifest.get('board') or ''
  chip=manifest.get('chip') or ''
  target={'ESP32-C3':'esp32c3','ESP32-S3':'esp32s3','ESP32-C6':'esp32c6'}.get(chip,'')
  return {'id':out.name,'status':'success','progress':100,'log':'','manifest':manifest,
          'cache_key':manifest.get('cache_key',''),
          'created_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(out.stat().st_mtime)),
          'request':{'board':'','name':board,'target':target,'language':'','wake_word':'','build_options':{}}}

def load_jobs():
  OUT.mkdir(parents=True,exist_ok=True)
  for out in sorted(OUT.iterdir()):
    if not out.is_dir(): continue
    record=None
    try:
      record=json.loads((out/'job.json').read_text())
      record.setdefault('id',out.name)
      record.setdefault('created_at',time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(out.stat().st_mtime)))
      if record.get('status') == 'success' and (out/'manifest.json').is_file():
        record['manifest']=json.loads((out/'manifest.json').read_text())
        req=record.get('request') or {}
        man=record['manifest']
        if not req.get('name'): req['name']=man.get('board','')
        if not req.get('target'):
          req['target']={'ESP32-C3':'esp32c3','ESP32-S3':'esp32s3','ESP32-C6':'esp32c6'}.get(man.get('chip',''),'')
        record['request']=req
    except (OSError,ValueError,KeyError):
      if (out/'manifest.json').is_file():
        try: record=_manifest_record(out)
        except (OSError,ValueError): record=None
    if record and record.get('status') in ('queued','running'):
      # Compilation runs in this process, so a restart kills it. Without this the
      # record would stay "running" forever: no artifact, no delete, no retry.
      record.update(status='failed',error='编译服务重启，任务已中断，请重新编译',progress=100,
                    finished_at=record.get('finished_at') or now_iso())
      try:
        (out/'job.json').write_text(json.dumps({k:v for k,v in record.items() if k!='manifest'},ensure_ascii=False,indent=2),encoding='utf-8')
      except OSError:
        pass
    if record and record.get('id'): JOBS[record['id']]=record

REQUIRED_ARTIFACTS=('merged-binary.bin','pico.bin')

def find_cached(key,body=None):
  with LOCK:
    for job in JOBS.values():
      if job.get('cache_key') != key or job.get('status') != 'success': continue
      out=OUT/job['id']; manifest=job.get('manifest')
      # Only a current-format manifest may back a cache hit. Older records store the
      # merged image inside `files`, which overlaps every real flash segment and would
      # be rejected by the browser flasher, so they are never reused.
      if not manifest or 'artifacts' not in manifest: continue
      downloadable={f.get('name') for f in manifest.get('artifacts',[])}
      segments=manifest.get('files') or []
      if set(REQUIRED_ARTIFACTS) <= downloadable and segments and \
         all((out/f['name']).is_file() for f in segments) and \
         all((out/f['name']).is_file() for f in manifest.get('artifacts',[])):
        cached=dict(job); cached['cached']=True
        if body is not None and not (cached.get('request') or {}).get('board'):
          cached['request']=request_of(body)
        return cached
  return None

def history():
  rows=[]
  for job in JOBS.values():
    manifest=job.get('manifest') or {}
    files=[]
    out=OUT/job['id']
    for f in manifest.get('artifacts') or manifest.get('files',[]):
      files.append({'name':f.get('name'),'size':f.get('size'),'sha256':f.get('sha256'),'available':(out/f.get('name','')).is_file()})
    present={f.get('name') for f in files}
    # Only current-format records are flashable: older ones list the merged image
    # among the flash segments, which the browser flasher rejects as overlapping.
    complete=('artifacts' in manifest) and set(REQUIRED_ARTIFACTS) <= present
    rows.append({'id':job.get('id'),'status':job.get('status'),'progress':job.get('progress',0),'created_at':job.get('created_at'),'finished_at':job.get('finished_at'),'cache_key':job.get('cache_key'),'request':job.get('request') or {},'chip':manifest.get('chip'),'files':files,'complete':complete,'error':job.get('error','')})
  rows.sort(key=lambda r:(r.get('created_at') or ''),reverse=True)
  return {'builds':rows}

def delete_build(job_id):
  """Remove one build completely: history record, artifacts on disk, and cache entry.

  A cached build is only reusable while its directory exists, so deleting the
  directory also retires the cache key and forces a fresh compile next time.
  """
  if not isinstance(job_id,str) or not re.fullmatch(r'[a-f0-9-]+',job_id):
    raise ValueError('invalid id')
  with LOCK:
    # Only a build with a live compile subprocess is protected. A record left in
    # "running" by a crash or restart has no process, so it must stay deletable.
    if job_id in ACTIVE:
      raise RuntimeError('build in progress')
  out=(OUT/job_id).resolve(); base=OUT.resolve()
  if out==base or not out.is_relative_to(base):
    raise ValueError('invalid id')
  freed=0; existed=False
  if out.is_dir():
    existed=True
    for f in out.rglob('*'):
      if f.is_file():
        try: freed+=f.stat().st_size
        except OSError: pass
    shutil.rmtree(out)
  with LOCK: removed=JOBS.pop(job_id,None) is not None
  if not existed and not removed:
    raise FileNotFoundError('not found')
  return {'deleted':job_id,'freed_bytes':freed}

def safe(v, pattern): return isinstance(v,str) and re.fullmatch(pattern,v) and '..' not in Path(v).parts

def run_job(job, body):
  try:
    with LOCK: ACTIVE.add(job['id'])
    job['status']='running'; job['progress']=10; out=OUT/job['id']; out.mkdir(parents=True,exist_ok=True); persist(job)
    opts=json.dumps(body.get('build_options') or {},ensure_ascii=False)
    cmd=['python3','scripts/build.py',body['board'],'--name',body['name'],'--language',body['language'],'--wake-word',body['wake_word'],'--build-options-json',opts]
    # Stream the compiler output. A first build can spend many minutes fetching
    # components; buffering everything until exit made the UI look frozen at 10%.
    tail=''; last_persist=0.0; code=1
    with open(out/'build.log','w',encoding='utf-8',errors='replace') as lf:
      proc=subprocess.Popen(cmd,cwd=ROOT,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,bufsize=1)
      for line in proc.stdout:
        lf.write(line); lf.flush()
        tail=(tail+line)[-200000:]; job['log']=tail
        m=re.search(r'\[(\d+)/(\d+)\]',line)
        if m:
          done,total=int(m.group(1)),int(m.group(2))
          if total: job['progress']=max(job.get('progress',10),min(60,10+int(50*done/total)))
        elif 'Building C objects' in line or 'Linking C' in line or 'Generating' in line:
          job['progress']=max(job.get('progress',10),70)
        now=time.time()
        if now-last_persist>=2:
          persist(job); last_persist=now
      code=proc.wait()
    job['progress']=max(job.get('progress',10),95); persist(job)
    if code: raise RuntimeError('builder exited '+str(code))
    # USB flashing uses the individual segments IDF defines; the merged image is a
    # download-only artifact and must stay out of `files` (it overlaps every segment).
    segments=[]
    for offset,name,src in flash_segments(ROOT):
      dst=out/name; shutil.copyfile(src,dst)
      segments.append({'name':name,'address':offset,'size':dst.stat().st_size,'sha256':sha256_of(dst)})
    # Download-only artifacts.
    artifacts=[]
    merged_src=ROOT/'build/merged-binary.bin'
    if not merged_src.is_file(): raise RuntimeError('build produced no merged-binary.bin')
    merged_dst=out/'merged-binary.bin'; shutil.copyfile(merged_src,merged_dst)
    artifacts.append({'name':'merged-binary.bin','size':merged_dst.stat().st_size,'sha256':sha256_of(merged_dst)})
    ota=[x for x in segments if x['name']=='pico.bin']
    if not ota: raise RuntimeError('build produced no pico.bin (OTA app image)')
    artifacts.append({'name':'pico.bin','size':ota[0]['size'],'sha256':ota[0]['sha256']})
    manifest={'schema':2,'product':'Pico','version':'pico','cache_key':job['cache_key'],'board':body['name'],'chip':{'esp32c3':'ESP32-C3','esp32s3':'ESP32-S3','esp32c6':'ESP32-C6'}.get(body['target'],'ESP32'),'flashSize':8388608,'files':segments,'artifacts':artifacts}
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8'); job.update(status='success',progress=100,finished_at=now_iso(),manifest=manifest); persist(job)
  except Exception as e: job.update(status='failed',error=str(e),progress=100,finished_at=now_iso()); persist(job)
  finally:
    with LOCK: ACTIVE.discard(job['id'])

def boards():
  def cmd(a): return json.loads(subprocess.check_output(['python3','scripts/build.py',a,'--json'],cwd=ROOT,text=True))
  variants=cmd('--list-boards')
  # Keep the compiler's canonical variant identity, while exposing the two
  # fields the official configurator shows separately.
  for v in variants:
    cfg=ROOT/'main'/'boards'/v['board']/'config.json'
    try:
      raw=json.loads(cfg.read_text())
      v['board_id']=v['name']
      v['screen_model']=raw.get('screen_model') or raw.get('display') or raw.get('screen') or ''
      if not v['screen_model']:
        selected = next((b for b in raw.get('builds',[]) if b.get('name') == v['name']), {})
        matches = [x[11:-2] for x in selected.get('sdkconfig_append',[]) if x.startswith('CONFIG_LCD_') and x.endswith('=y')]
        v['screen_model'] = matches[0] if matches else ''
      v['capabilities']=[]
      for option in v.get('build_options',[]):
        if option.get('key') in ('camera_hmirror','camera_vflip') or 'display' in option.get('key',''):
          v['capabilities'].append(option['key'])
    except (OSError,ValueError):
      v['board_id']=v['name']; v['screen_model']=''; v['capabilities']=[]
  return {'boards':variants,'languages':cmd('--list-languages')}
class Handler(BaseHTTPRequestHandler):
  def log_message(self,*a): pass
  def send(self,code,data,ctype='application/json'):
    raw=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode(); self.send_response(code); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
  def do_GET(self):
    if self.path=='/health':
      try:
        subprocess.run(['python3','scripts/build.py','--list-boards','--json'],cwd=ROOT,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.send(200,{'status':'ready','source':str(ROOT)})
      except Exception as e:
        self.send(503,{'status':'not_ready','error':str(e)})
      return
    if self.path=='/boards':
      try:self.send(200,boards())
      except Exception as e:self.send(500,{'error':str(e)})
      return
    if self.path=='/builds':
      with LOCK: self.send(200,history())
      return
    m=re.fullmatch(r'/build/([a-f0-9-]+)(?:/manifest)?',self.path)
    if m:
      j=JOBS.get(m.group(1));
      if not j:self.send(404,{'error':'not found'});return
      if self.path.endswith('/manifest') and j.get('manifest'):self.send(200,j['manifest']);return
      self.send(200,{k:v for k,v in j.items() if k!='manifest'});return
    m=re.fullmatch(r'/build/([a-f0-9-]+)/artifact/([A-Za-z0-9_.-]+)',self.path)
    if m:
      p=OUT/m.group(1)/m.group(2)
      if not p.is_file() or not p.resolve().is_relative_to((OUT/m.group(1)).resolve()):self.send(404,{'error':'not found'});return
      self.send(200,p.read_bytes(),'application/octet-stream');return
    self.send(404,{'error':'not found'})
  def do_DELETE(self):
    m=re.fullmatch(r'/build/([a-f0-9-]+)',self.path)
    if not m:self.send(404,{'error':'not found'});return
    try:self.send(200,delete_build(m.group(1)))
    except FileNotFoundError:self.send(404,{'error':'not found'})
    except RuntimeError as e:self.send(409,{'error':str(e)})
    except ValueError as e:self.send(400,{'error':str(e)})
    except OSError as e:self.send(500,{'error':str(e)})
  def do_POST(self):
    if self.path!='/build':self.send(404,{'error':'not found'});return
    try:
      b=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))));
      for k in ('board','name','target','language','wake_word'):
        if not isinstance(b.get(k),str) or not b[k]:raise ValueError('missing '+k)
      if not safe(b['board'],r'[a-z0-9][a-z0-9._/-]*') or not safe(b['name'],r'[a-z0-9][a-z0-9.-]*'):raise ValueError('invalid board')
      key=cache_key(b)
      cached=find_cached(key,b)
      if cached: self.send(200,cached); return
      i=str(uuid.uuid4());j={'id':i,'status':'queued','progress':0,'log':'','cache_key':key,'created_at':now_iso(),'request':request_of(b)};
      with LOCK: JOBS[i]=j; ACTIVE.add(i)
      persist(j); threading.Thread(target=run_job,args=(j,b),daemon=True).start(); self.send(202,j)
    except Exception as e:self.send(400,{'error':str(e)})
def serve():
  load_jobs()
  ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('FIRMWARE_BUILDER_PORT','8090'))),Handler).serve_forever()

if __name__=='__main__':
  serve()
