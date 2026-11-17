"""Exercise Gateway API/TLS with disposable fixture upstreams only.

This checks the rendered edge and actual Envoy controller, not a full production
Sentinel rollout, payment integration or multi-zone availability.
"""
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import time
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
CONTEXT='kind-sentinel-gateway'
NAMESPACE='sentinel-ci-routing'
HOST='sentinel.invalid'


def validate_target(context,server):
    parsed=urlsplit(server)
    if context!=CONTEXT or parsed.scheme!='https' or parsed.username or parsed.password or parsed.fragment or parsed.hostname not in ('127.0.0.1','localhost','::1') or not parsed.port:
        raise RuntimeError('Only the named disposable loopback kind cluster is allowed')


def command(args,*,input=None,sensitive=False,timeout=60):
    result=subprocess.run(args,cwd=ROOT,input=input,capture_output=True,timeout=timeout)
    if result.returncode:
        detail='' if sensitive else result.stderr.decode(errors='replace')[-2500:]
        raise RuntimeError('Gateway CI command failed: '+args[0]+' '+detail)
    return result.stdout


def kube(*args,input=None,sensitive=False,timeout=60):
    return command(['kubectl','--context',CONTEXT,*args],input=input,sensitive=sensitive,timeout=timeout)


def apply(items):
    kube('apply','-f','-',input=json.dumps({'apiVersion':'v1','kind':'List','items':items}).encode())


def wait_for(fn,timeout=120):
    end=time.monotonic()+timeout
    last=None
    while time.monotonic()<end:
        try:
            result=fn()
            if result:return result
        except (OSError,RuntimeError,ValueError,http.client.HTTPException) as exc:
            last=type(exc).__name__
        time.sleep(1)
    raise RuntimeError('Gateway CI condition did not become ready ('+str(last)+')')


def available_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


def fixture_resources(resources,image):
    # Deploy the rendered reverse proxy with fixture upstreams. Full project
    # images, migrations and HA placement have a separate validation scope.
    selected=[]
    for row in resources:
        kind,name=row['kind'],row['metadata']['name']
        if kind in ('Namespace','Gateway','HTTPRoute','NetworkPolicy') or kind=='ConfigMap' and name=='sentinel-gateway-config':
            selected.append(row)
        elif kind=='Service':
            if name!='sentinel-gateway':row['spec']['selector']={'app':'sentinel-fixture'}
            selected.append(row)
        elif kind=='Deployment' and name=='sentinel-gateway':
            row['spec']['replicas']=1
            row['spec']['template']['spec'].pop('affinity')
            selected.append(row)
    nginx='''pid /tmp/nginx.pid;
events { worker_connections 64; }
http { client_body_temp_path /tmp/client; proxy_temp_path /tmp/proxy;
 fastcgi_temp_path /tmp/fastcgi; uwsgi_temp_path /tmp/uwsgi; scgi_temp_path /tmp/scgi;
 server { listen 8000; location / { return 200 "api fixture"; } }
 server { listen 8080; location / { return 200 "frontend fixture"; } }
}'''
    selected.append({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'sentinel-fixture','namespace':NAMESPACE},'data':{'nginx.conf':nginx}})
    selected.append({'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'sentinel-fixture','namespace':NAMESPACE},'spec':{'replicas':1,'selector':{'matchLabels':{'app':'sentinel-fixture'}},'template':{'metadata':{'labels':{'app':'sentinel-fixture'}},'spec':{'automountServiceAccountToken':False,'securityContext':{'runAsNonRoot':True,'runAsUser':101,'seccompProfile':{'type':'RuntimeDefault'}},'containers':[{'name':'fixture','image':image,'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},'resources':{'requests':{'cpu':'50m','memory':'32Mi'},'limits':{'cpu':'250m','memory':'128Mi'}},'volumeMounts':[{'name':'tmp','mountPath':'/tmp'},{'name':'config','mountPath':'/etc/nginx/nginx.conf','subPath':'nginx.conf','readOnly':True}]}],'volumes':[{'name':'tmp','emptyDir':{}},{'name':'config','configMap':{'name':'sentinel-fixture'}}]}}}})
    return selected


def main():
    if os.environ.get('SENTINEL_GATEWAY_ACCEPTANCE')!='1':
        raise SystemExit('Explicit isolated gateway acceptance opt-in is required')
    server=kube('config','view','--minify','-o','jsonpath={.clusters[0].cluster.server}').decode()
    validate_target(CONTEXT,server)
    image=os.environ.get('SENTINEL_GATEWAY_FIXTURE_IMAGE','')
    spec=importlib.util.spec_from_file_location('sentinel_render',ROOT/'deploy/kubernetes/render.py')
    renderer=importlib.util.module_from_spec(spec);spec.loader.exec_module(renderer)
    resources=renderer.render(HOST,dict.fromkeys(('backend','frontend','gateway'),image),NAMESPACE,gateway_class='sentinel-ci')
    artifacts=ROOT/'artifacts';artifacts.mkdir(exist_ok=True)
    certs=artifacts/'gateway-certs';certs.mkdir(exist_ok=True)
    ca,ca_key=certs/'ca.crt',certs/'ca.key'
    command(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(ca_key),'-out',str(ca),'-days','1','-subj','/CN=Sentinel Disposable CI CA'])
    (certs/'extensions').write_text('subjectAltName=DNS:'+HOST+'\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n')
    def rotate():
        key,csr,crt=certs/'server.key',certs/'server.csr',certs/'server.crt'
        command(['openssl','req','-new','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(csr),'-subj','/CN='+HOST])
        command(['openssl','x509','-req','-in',str(csr),'-CA',str(ca),'-CAkey',str(ca_key),'-CAcreateserial','-out',str(crt),'-days','1','-extfile',str(certs/'extensions')])
        raw=kube('-n',NAMESPACE,'create','secret','tls','sentinel-tls','--cert='+str(crt),'--key='+str(key),'--dry-run=client','-o','json',sensitive=True)
        kube('apply','-f','-',input=raw,sensitive=True)
    platform=[{'apiVersion':'gateway.envoyproxy.io/v1alpha1','kind':'EnvoyProxy','metadata':{'name':'sentinel-ci','namespace':'envoy-gateway-system'},'spec':{'provider':{'type':'Kubernetes','kubernetes':{'envoyService':{'type':'ClusterIP'}}}}},
        {'apiVersion':'gateway.networking.k8s.io/v1','kind':'GatewayClass','metadata':{'name':'sentinel-ci'},'spec':{'controllerName':'gateway.envoyproxy.io/gatewayclass-controller','parametersRef':{'group':'gateway.envoyproxy.io','kind':'EnvoyProxy','name':'sentinel-ci','namespace':'envoy-gateway-system'}}}]
    apply(platform)
    apply([next(row for row in resources if row['kind']=='Namespace')])
    rotate()
    apply(fixture_resources(resources,image))
    for deployment in ('sentinel-fixture','sentinel-gateway'):
        kube('-n',NAMESPACE,'rollout','status','deployment/'+deployment,'--timeout=120s',timeout=130)
    report={'scope':'disposable Gateway API / Envoy / rendered nginx / fixture upstreams; not full Sentinel production rollout','checks':{},'gateway_class':'sentinel-ci','fixture_image':image,'controller_version':'v1.10.0'}
    def check(name,ok):
        if not ok:raise AssertionError(name)
        report['checks'][name]='pass';print(name+': pass',flush=True)
    def routes_ready():
        for name in ('sentinel-https','sentinel-http-redirect'):
            route=json.loads(kube('-n',NAMESPACE,'get','httproute',name,'-o','json'))
            parents=route.get('status',{}).get('parents',[])
            if not any(all(any(condition['type']==kind and condition['status']=='True' and condition.get('observedGeneration')==route['metadata']['generation'] for condition in parent.get('conditions',[])) for kind in ('Accepted','ResolvedRefs')) for parent in parents):return False
        return True
    check('gateway_routes_accepted_and_references_resolved',wait_for(routes_ready))
    def service():
        data=json.loads(kube('get','services','-A','-l','gateway.envoyproxy.io/owning-gateway-namespace='+NAMESPACE+',gateway.envoyproxy.io/owning-gateway-name=sentinel-edge','-o','json'))
        return data['items'][0] if data['items'] else None
    svc=wait_for(service)
    http_port,tls_port=available_port(),available_port()
    log=(artifacts/'gateway-forward.log').open('wb')
    forwarded=subprocess.Popen(['kubectl','--context',CONTEXT,'-n',svc['metadata']['namespace'],'port-forward','service/'+svc['metadata']['name'],str(http_port)+':80',str(tls_port)+':443','--address=127.0.0.1','--pod-running-timeout=120s'],cwd=ROOT,stdout=log,stderr=log)
    def plain(path,host=HOST):
        conn=http.client.HTTPConnection('127.0.0.1',http_port,timeout=5)
        try:
            conn.request('GET',path,headers={'Host':host});response=conn.getresponse();body=response.read(4096)
            return response.status,{key.lower():value for key,value in response.getheaders()},body
        finally:conn.close()
    def secure(path,host=HOST,trusted=True):
        context=ssl.create_default_context(cafile=str(ca)) if trusted else ssl.create_default_context()
        conn=http.client.HTTPSConnection(HOST,timeout=5,context=context)
        raw=socket.create_connection(('127.0.0.1',tls_port),timeout=5)
        try:
            conn.sock=context.wrap_socket(raw,server_hostname=HOST)
            fingerprint=hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
            conn.request('GET',path,headers={'Host':host});response=conn.getresponse();body=response.read(4096)
            return response.status,{key.lower():value for key,value in response.getheaders()},body,fingerprint
        finally:
            conn.close();raw.close()
    try:
        redirect=wait_for(lambda:(result if (result:=plain('/app?test=1'))[0]==301 else None))
        check('http_redirects_to_exact_https_host',redirect[1].get('location')=='https://'+HOST+'/app?test=1')
        frontend=wait_for(lambda:(result if (result:=secure('/'))[0]==200 and result[2]==b'frontend fixture' else None))
        check('https_certificate_validated_and_fixture_frontend_routed',frontend[0]==200)
        check('https_hsts_header',frontend[1].get('strict-transport-security')=='max-age=31536000')
        api=secure('/api/ping')
        check('https_api_prefix_routed',api[0]==200 and api[2]==b'api fixture')
        check('public_health_is_gateway_only',secure('/health')[2]==b'ok')
        check('dependency_probes_not_public',all(secure(path)[0]==404 for path in ('/api/health/ready','/api//health/ready','/api/%68ealth/ready')))
        check('unknown_http_host_not_redirected',plain('/',host='other.invalid')[0] in (404,421))
        check('mismatched_https_host_rejected',secure('/',host='other.invalid')[0] in (404,421))
        rejected=False
        try:secure('/',trusted=False)
        except ssl.SSLCertVerificationError:rejected=True
        check('untrusted_certificate_rejected',rejected)
        old=frontend[3]
        rotate()
        updated=wait_for(lambda:(result if (result:=secure('/'))[0]==200 and result[3]!=old else None))
        check('tls_secret_rotation_reloaded',updated[3]!=old)
        (artifacts/'gateway-acceptance.json').write_text(json.dumps(report,indent=2)+'\n')
        print('GATEWAY_ACCEPTANCE_PASS',flush=True)
    finally:
        forwarded.terminate()
        try:forwarded.wait(timeout=5)
        except subprocess.TimeoutExpired:forwarded.kill();forwarded.wait(timeout=5)
        log.close()


if __name__=='__main__':main()
