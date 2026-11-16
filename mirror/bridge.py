import mimetypes,json,os,hmac,http.client,time,hashlib,threading,secrets
from pathlib import Path
from urllib.parse import urlencode
from flask import Flask,request,Response,stream_with_context,redirect
from curl_cffi import requests
from session_pool import pool
from itsdangerous import URLSafeTimedSerializer,BadSignature
from werkzeug.exceptions import HTTPException
from werkzeug.http import parse_options_header
app=Flask(__name__)
app.config['MAX_CONTENT_LENGTH']=100*1024**2
ASSETS=Path(__file__).resolve().parent
STATE_DIR=Path(os.environ.get('MIRROR_STATE_DIR','/var/lib/claude-mirror'))
CONF=STATE_DIR/'accounts.json'
PARENT_ORIGIN=os.environ.get('MIRROR_PARENT_ORIGIN','https://localhost:9443')
FRAME_HOST=os.environ.get('MIRROR_FRAME_HOST','localhost')
FRAME_SHELL=Path(os.environ.get('MIRROR_FRAME_SHELL',str(ASSETS/'frame-shell.js')))
FRAME_FIRST=int(os.environ.get('MIRROR_FRAME_FIRST','11443'))
FRAME_COUNT=int(os.environ.get('MIRROR_FRAME_COUNT','256'))
UI_PORT=int(os.environ.get('MIRROR_UI_PORT','8181'))
HOP={'connection','transfer-encoding','content-encoding','content-length','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','upgrade','set-cookie','alt-svc','content-security-policy','content-security-policy-report-only'}
AUTH_CACHE={};AUTH_LOCK=threading.Lock();CONF_LOCK=threading.Lock()
BLOCKCOOKIE={'gpt_site','mirror_control','sessionKey','sessionOrigin','session','_Secure-next-auth.session-data','_Secure-next-auth.site-password','mirror_choice','cf_clearance','__cf_bm','_cfuvid'}

def config():return json.loads(CONF.read_text())
def write_config(c):
 tmp=CONF.with_suffix('.tmp');tmp.write_text(json.dumps(c,ensure_ascii=False));tmp.chmod(0o600);tmp.replace(CONF)
def site_allowed():
 site_cookie=request.cookies.get('_Secure-next-auth.site-password','')
 if not site_cookie:return False
 digest=hashlib.sha256(site_cookie.encode()).hexdigest()
 with AUTH_LOCK:cached=AUTH_CACHE.get(digest,0)>time.time()
 if not cached:
  conn=http.client.HTTPConnection('127.0.0.1',UI_PORT,timeout=10)
  try:
   conn.request('GET','/login',headers={'Host':request.host,'Cookie':'_Secure-next-auth.site-password='+site_cookie})
   check=conn.getresponse();location=check.getheader('Location','');status=check.status;check.read()
  except Exception:return False
  finally:conn.close()
  if not (status==200 or (status==302 and '/site/login' not in location)):return False
  with AUTH_LOCK:
   if len(AUTH_CACHE)>1000:AUTH_CACHE.clear()
   AUTH_CACHE[digest]=time.time()+30
 return True

def signer(c):return URLSafeTimedSerializer(c['bridge_secret'],salt='mirror-account-choice')
def selected(c):
 try:ident=signer(c).loads(request.cookies.get('mirror_choice',''),max_age=30*86400)
 except BadSignature:ident=c['default_account']
 return next((a for a in c['accounts'] if a['id']==ident),next(iter(c['accounts']),None))
def csrf(c):return hmac.new(c['bridge_secret'].encode(),request.cookies.get('_Secure-next-auth.site-password','').encode(),hashlib.sha256).hexdigest()
@app.before_request
def guard():
 c=config()
 if not hmac.compare_digest(request.headers.get('X-Mirror-Bridge',''),c['bridge_secret']):return 'Forbidden',403
 if request.path=='/mirror/ui.js' or request.path.startswith('/__mirror_content/'):return
 if not site_allowed():
  if request.path in {'/mirror/accounts','/mirror/resume','/login'}:return redirect('/site/login?'+urlencode({'returnTo':request.full_path.rstrip('?')}))
  return {'error':'请先输入站点密码'},401
 if request.path.startswith('/mirror/') and request.method!='GET':
  if not hmac.compare_digest(request.headers.get('X-Mirror-CSRF',''),csrf(c)):return {'error':'页面已过期，请刷新'},403
  if request.headers.get('Origin')!='https://'+request.host:return {'error':'请求来源不正确'},403
@app.errorhandler(Exception)
def manager_error(exc):
 if request.path.startswith('/mirror/'):
  code=exc.code if isinstance(exc,HTTPException) else 500
  app.logger.error('Account manager failure: %s',type(exc).__name__)
  return {'error':'账号管理操作失败，请稍后重试' if code>=500 else '请求无效，请刷新页面后重试'},code
 if isinstance(exc,HTTPException):return exc
 app.logger.error('Bridge failure: %s',type(exc).__name__)
 return {'error':'转发服务暂时不可用，请稍后重试'},500
@app.get('/login')
@app.get('/mirror/resume')
def resume():
 c=config();a=selected(c)
 if not a:return redirect('/mirror/accounts',code=302)
 conn=http.client.HTTPConnection('127.0.0.1',UI_PORT,timeout=30)
 try:
  conn.request('GET','/login_token?'+urlencode({'session_key':a['session_key']}),headers={'Host':request.host,'Cookie':request.headers.get('Cookie','')})
  upstream=conn.getresponse();cookies=[v for k,v in upstream.getheaders() if k.lower()=='set-cookie'];upstream.read()
 finally:conn.close()
 response=redirect('/new',code=302);response.headers['Cache-Control']='no-store'
 for cookie in cookies:
  if 'secure' not in cookie.lower():cookie+='; Secure'
  response.headers.add('Set-Cookie',cookie)
 response.set_cookie('mirror_choice',signer(c).dumps(a['id']),max_age=30*86400,secure=True,httponly=True,samesite='Lax')
 return response
@app.get('/mirror/ui.js')
def ui_script():return Response((ASSETS/'account-ui.js').read_text(encoding='utf-8'),content_type='application/javascript',headers={'Cache-Control':'no-store'})
@app.get('/mirror/accounts')
def panel():return Response((ASSETS/'accounts.html').read_text(encoding='utf-8'),content_type='text/html',headers={'Cache-Control':'no-store'})
@app.get('/mirror/state')
def state():
 c=config();current=selected(c);return {'accounts':[{'id':a['id'],'name':a['name']} for a in c['accounts']],'selected':current['id'] if current else None,'csrf':csrf(c)}
@app.post('/mirror/switch')
def switch():
 c=config();ident=(request.get_json(silent=True) or {}).get('id','')
 if not any(a['id']==ident for a in c['accounts']):return {'error':'账号不存在'},404
 r=app.json.response({'next':'/mirror/resume'});r.set_cookie('mirror_choice',signer(c).dumps(ident),max_age=30*86400,secure=True,httponly=True,samesite='Lax');r.headers['Cache-Control']='no-store';return r
@app.post('/mirror/add')
def add():
 data=request.get_json(silent=True) or {};key=data.get('sessionKey','').strip();name=data.get('name','').strip()[:40]
 if not key.startswith('sk-ant-sid') or len(key)>4096:return {'error':'请输入完整的 Claude sessionKey'},400
 c=config()
 if len(c['accounts'])>=10:return {'error':'最多保存 10 个账号'},400
 try:
  with requests.Session(impersonate='chrome') as sess:
   check=sess.get('https://claude.ai/api/organizations',cookies={'sessionKey':key},timeout=30)
   if check.status_code!=200:return {'error':'凭证验证失败，请确认已在 Claude 官网登录并复制完整 sessionKey'},400
   orgs=check.json()
   if not isinstance(orgs,list) or not orgs:return {'error':'没有找到可用的 Claude 账号'},400
 except Exception:return {'error':'暂时无法连接官网，请稍后重试'},502
 with CONF_LOCK:
  c=config()
  existing=next((a for a in c['accounts'] if a['session_key']==key),None)
  if existing:return {'error':'这个会话已经保存，可直接切换到对应账号'},409
  ident=secrets.token_hex(16);c['accounts'].append({'id':ident,'name':name or '账号 '+str(len(c['accounts'])+1),'session_key':key})
  if not c['default_account']:c['default_account']=ident
  write_config(c)
 return {'ok':True}
@app.post('/mirror/rename')
def rename():
 data=request.get_json(silent=True) or {};name=data.get('name','').strip()[:40]
 if not name:return {'error':'请输入账号名称'},400
 with CONF_LOCK:
  c=config();a=next((a for a in c['accounts'] if a['id']==data.get('id')),None)
  if not a:return {'error':'账号不存在'},404
  a['name']=name;write_config(c)
 return {'ok':True}
@app.post('/mirror/remove')
def remove():
 ident=(request.get_json(silent=True) or {}).get('id')
 with CONF_LOCK:
  c=config()
  if len(c['accounts'])<=1:return {'error':'至少保留一个账号'},400
  c['accounts']=[a for a in c['accounts'] if a['id']!=ident]
  if c['default_account']==ident:c['default_account']=c['accounts'][0]['id']
  write_config(c)
 return {'ok':True}
@app.post('/mirror/lock')
def lock():
 r=app.json.response({'next':'/site/login?returnTo=%2Flogin'})
 for cookie in ['_Secure-next-auth.site-password','_Secure-next-auth.session-data','sessionKey','sessionOrigin']:r.delete_cookie(cookie,secure=True,httponly=True,samesite='Lax')
 return r

import re
from urllib.parse import urlsplit
FRAME_MAP=STATE_DIR/'frame-origins.json'
FRAME_LOCK=threading.Lock()
FRAME_BASE='https://assets-proxy.anthropic.com/claude-ai/v2/assets/v1/'
def frame_mapping():
 return json.loads(FRAME_MAP.read_text()) if FRAME_MAP.exists() else {}
def frame_port(ident):
 with FRAME_LOCK:
  data=frame_mapping()
  if ident in data:return data[ident]
  free=next((v for v in range(FRAME_FIRST,FRAME_FIRST+FRAME_COUNT) if v not in data.values()),None)
  if free is None:raise RuntimeError('Document origin limit')
  data[ident]=free;tmp=FRAME_MAP.with_suffix('.tmp');tmp.write_text(json.dumps(data));tmp.chmod(0o600);tmp.replace(FRAME_MAP);return free
@app.get('/mirror/frame-shell.js')
def frame_script():
 port=request.args.get('port',type=int)
 if port not in frame_mapping().values():return 'Not found',404
 raw=FRAME_SHELL.read_text(encoding='utf-8')
 raw=raw.replace('${go()}//${Xc}','https://'+FRAME_HOST+':'+str(port))
 raw=re.sub(r'(["\'])\./([^"\']+\.js)\1',lambda m:json.dumps(FRAME_BASE+m[2]),raw)
 return Response(raw,content_type='application/javascript',headers={'Cache-Control':'no-store'})
@app.get('/code/artifact/<ident>')
@app.get('/code/frame/<ident>')
def frame_shell(ident):
 if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',ident):return 'Not found',404
 a=selected(config())
 if not a:return redirect('/mirror/accounts',code=302)
 port=frame_port(ident)
 with requests.Session(impersonate='chrome') as sess:
  res=sess.get('https://claude.ai'+request.path,params=list(request.args.items(multi=True)),cookies={'sessionKey':a['session_key']},timeout=60,allow_redirects=False)
  raw=res.content
  if res.status_code==200:
   raw=re.sub(rb'https://assets-proxy\.anthropic\.com/claude-ai/v2/assets/v1/frame-shell-[A-Za-z0-9_-]+\.js',('/mirror/frame-shell.js?port='+str(port)).encode(),raw)
  return Response(raw,status=res.status_code,headers={k:v for k,v in res.headers.items() if k.lower() not in HOP and k.lower()!='x-frame-options'})
@app.route('/__mirror_content/<int:port>/',defaults={'path':''},methods=['GET','POST','OPTIONS'])
@app.route('/__mirror_content/<int:port>/<path:path>',methods=['GET','POST','OPTIONS'])
def isolated_content(port,path):
 ident=next((k for k,v in frame_mapping().items() if v==port),None)
 if not ident or not (path in {'_t','_warmup'} or path.startswith(('_f/','_runtime/'))):return 'Not found',404
 host=ident+'.frame.claudeusercontent.com';prefix='__Secure-mframe'+str(port)+'-'
 cookies={k[len(prefix):]:v for k,v in request.cookies.items() if k.startswith(prefix)}
 headers={k:v for k,v in request.headers if k.lower() in {'accept','accept-language','content-type','range','if-none-match','if-modified-since'}}
 headers.update({'Referer':'https://claude.ai/code/artifact/'+ident,'Sec-Fetch-Site':'same-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'iframe'})
 sess=pool.acquire()
 try:res=sess.request(request.method,'https://'+host+'/'+path,params=list(request.args.items(multi=True)),headers=headers,cookies=cookies,data=request.get_data(),stream=True,discard_cookies=True,allow_redirects=False,timeout=(30,120))
 except Exception:pool.release(sess,broken=True);return 'Content unavailable',502
 rh={k:v for k,v in res.headers.items() if k.lower() not in HOP and k.lower()!='x-frame-options'}
 csp=res.headers.get('Content-Security-Policy','')
 if csp:rh['Content-Security-Policy']=csp.replace("frame-ancestors 'self'", "frame-ancestors 'self' "+PARENT_ORIGIN)
 location=res.headers.get('Location','')
 if location.startswith('https://'+host+'/'):rh['Location']=location.split(host,1)[1]
 elif location.startswith('https://claude.ai/'):rh['Location']=location.replace('https://claude.ai',PARENT_ORIGIN,1)
 # Only content grant cookies cross this boundary; account cookies never do.
 content_type=res.headers.get('Content-Type','')
 if 'text/html' in content_type or (path.endswith('.js') and ('/artifact-type/' in path or path.startswith('_runtime/'))):
  origin=PARENT_ORIGIN.encode('ascii')
  raw=b''.join(res.iter_content()).replace(b'["https://claude.ai","https://preview.claude.ai"]',b'["'+origin+b'"]').replace(b'https://claude.ai',origin)
  if 'text/html' in content_type:
   raw=raw.replace(b'window.__FRAME_PREAMBLE={',('window.__FRAME_PREAMBLE={"container":"project/'+ident+'","origins":'+json.dumps([PARENT_ORIGIN])+',').encode(),1)
  response=Response(raw,status=res.status_code,headers=rh);res.close();pool.release(sess)
 else:
  @stream_with_context
  def body():
   try:yield from res.iter_content()
   finally:res.close();pool.release(sess)
  response=Response(body(),status=res.status_code,headers=rh)
 for cookie in res.headers.get_list('set-cookie'):
  name,sep,rest=cookie.partition('=')
  if sep and name.startswith('__Host-frame-'):
   response.headers.add('Set-Cookie',prefix+name+'='+re.sub(r';\s*Domain=[^;]+','',rest,flags=re.I))
 response.headers['Cache-Control']='private, max-age=86400, immutable' if res.status_code==200 and path.endswith('.js') and ('/artifact-type/' in path or path.startswith('_runtime/')) else 'no-store'
 return response

@app.route('/',defaults={'path':''},methods=['GET','POST','PUT','DELETE','PATCH','OPTIONS'])
@app.route('/<path:path>',methods=['GET','POST','PUT','DELETE','PATCH','OPTIONS'])
def proxy(path):
 if not (path.startswith('api/') or path.startswith('edge-api/') or path.startswith('v1/') or path.startswith('claudeai-rpc/')):return 'Not found',404
 c=config();a=selected(c)
 if not a:return {'error':'请先在账号管理中添加账号'},401
 headers={k:v for k,v in request.headers if k.lower() in {'accept','accept-language','content-type','range','if-match','if-none-match','x-csrf-token','x-app-locale','x-organization-uuid','authorization'} or k.lower().startswith(('anthropic-','x-anthropic-','connect-','x-client-','x-claude-','x-frame-'))}
 if request.method not in {'GET','HEAD','OPTIONS'}:headers['Origin']='https://claude.ai'
 if path.startswith(('api/frame/','api/artifacts/')):
  from urllib.parse import urlsplit
  ref=urlsplit(request.headers.get('Referer',''))
  headers['Referer']='https://claude.ai'+(ref.path or '/')+('?' + ref.query if ref.query else '')
  headers['Sec-Fetch-Site']='same-origin';headers['Sec-Fetch-Mode']='cors';headers['Sec-Fetch-Dest']='empty'
  if request.args.get('org'):headers.setdefault('X-Organization-Uuid',request.args['org'])
 cookies={k:v for k,v in request.cookies.items() if k not in BLOCKCOOKIE};cookies['sessionKey']=a['session_key']
 buffered=(request.method=='GET' and path in {'api/organizations','api/account','api/bootstrap'})
 sess=pool.acquire()
 try:
  res=sess.request(request.method,'https://claude.ai/'+path,params=list(request.args.items(multi=True)),headers=headers,cookies=cookies,data=request.get_data(),stream=not buffered,discard_cookies=True,allow_redirects=False,timeout=(30,600))
 except Exception:
  pool.release(sess,broken=True)
  return {'error':'无法连接 Claude 官网，请稍后重试'},502
 content_type=res.headers.get('content-type','').lower()
 if res.headers.get('cf-mitigated')=='challenge' or ('text/html' in content_type and res.status_code>=400):
  res.close();pool.release(sess)
  return {'type':'error','error':{'type':'proxy_upstream_error','message':'Claude 官网返回了验证页面或非 JSON 错误，请稍后重试'}},502
 if res.status_code in {301,302,303,307,308} and '/login' in res.headers.get('location',''):
  res.close();pool.release(sess)
  return {'type':'error','error':{'type':'authentication_error','message':'Claude 会话已失效，请更新此账号的 sessionKey'}},401
 rh={k:v for k,v in res.headers.items() if k.lower() not in HOP};location=res.headers.get('location')
 if location and location.startswith('https://claude.ai/'):rh['location']=location.replace('https://claude.ai','https://'+request.host,1)
 # Flask otherwise labels headerless binary file responses as HTML.
 if not res.headers.get('content-type'):
  _,options=parse_options_header(res.headers.get('content-disposition',''))
  rh['Content-Type']=mimetypes.guess_type(options.get('filename',''))[0] or 'application/octet-stream'
 rh['X-Mirror-Bridge']='direct-web-session';rh['Cache-Control']='no-store'
 @stream_with_context
 def body():
  try:
   for chunk in ([res.content] if buffered else res.iter_content()):yield chunk
  finally:res.close();pool.release(sess)
 return Response(body(),status=res.status_code,headers=rh)


from flask_sock import Sock
from curl_cffi.const import CurlWsFlag
app.config['SOCK_SERVER_OPTIONS']={'subprotocols':['frame-sync.v1'],'ping_interval':25,'max_message_size':16*1024*1024}
sock=Sock(app)
@sock.route('/api/frame/sync')
def document_sync(client):
 if request.headers.get('Origin')!='https://'+request.host:
  client.close(reason=1008,message='Invalid origin');return
 account=selected(config())
 if not account:
  client.close(reason=1008,message='No account selected');return
 up=None;done=threading.Event()
 sess=requests.Session(impersonate='chrome')
 try:
  up=sess.ws_connect('wss://claude.ai/api/frame/sync?'+urlencode(list(request.args.items(multi=True))),headers={'Origin':'https://claude.ai','Referer':'https://claude.ai/','Sec-WebSocket-Protocol':request.headers.get('Sec-WebSocket-Protocol','frame-sync.v1')},cookies={'sessionKey':account['session_key']},timeout=30,impersonate='chrome')
  def incoming():
   try:
    while not done.is_set():
     data,flags=up.recv()
     if flags & CurlWsFlag.CLOSE:break
     if flags & CurlWsFlag.TEXT:client.send(data.decode('utf-8'))
     elif flags & CurlWsFlag.BINARY:client.send(data)
   except Exception:pass
   finally:
    done.set()
    try:client.close()
    except Exception:pass
  reader=threading.Thread(target=incoming,daemon=True);reader.start()
  while not done.is_set():
   data=client.receive(timeout=1)
   if data is not None:up.send(data,flags=CurlWsFlag.TEXT if isinstance(data,str) else CurlWsFlag.BINARY)
 except Exception as exc:
  app.logger.warning('Document websocket closed: %s',type(exc).__name__)
 finally:
  done.set()
  if up:
   try:up.close()
   except Exception:pass
  sess.close()
  try:client.close()
  except Exception:pass
