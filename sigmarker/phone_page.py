"""离线内置手机上传页面；不访问 CDN 或其他外部服务。"""

PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>上传手写签名 · SigMarker</title>
<style>
*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;background:#f2f5f8;color:#203044;
font:17px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;padding:24px 18px}
main{max-width:480px;margin:5vh auto;background:white;padding:28px 22px;border-radius:18px;
box-shadow:0 8px 30px #20304410}h1{font-size:26px;margin:10px 0}p{margin:12px 0}
.brand{color:#52677c;font-size:14px;letter-spacing:1px}.hint{color:#596a7c;font-size:14px}
button{display:block;width:100%;border:0;border-radius:10px;padding:15px;margin:12px 0;
font:inherit;background:#1669c9;color:white}button.secondary{background:#e9f1fa;color:#174f8c}
button:disabled{opacity:.5}#status{padding:14px;border-radius:10px;background:#f0f4f8;
white-space:pre-wrap;overflow-wrap:anywhere}#status.error{color:#a12424;background:#fff0f0}
#status.success{color:#19613a;background:#eaf8ef}input{display:none}
</style></head><body><main>
<div class="brand">SigMarker · 手机扫码导入</div><h1>上传手写签名</h1>
<p>拍摄白纸上的签名，或选择相册照片。</p>
<button id="camera" disabled>拍照</button>
<button id="album" class="secondary" disabled>选择图片</button>
<input id="cameraFile" type="file" accept="image/*" capture="environment">
<input id="albumFile" type="file" accept="image/jpeg,image/png,image/heic,image/heif,.jpg,.jpeg,.png,.heic,.heif">
<p id="status" role="status" aria-live="polite">正在连接电脑…</p>
<button id="retry" class="secondary" hidden>检查连接 / 重试</button>
<p class="hint">支持 JPEG、PNG、HEIC/HEIF。每张最多 30 MiB、6000 万像素，一次扫码接收一张。</p>
<p class="hint">手机和电脑需要处于可互访的局域网。HTTP 传输未加密，请仅在可信网络使用。</p>
</main><script>
'use strict';
const token = new URLSearchParams(location.hash.slice(1)).get('token') || '';
const el = id => document.getElementById(id);
let selected = null, completed = false, busy = false;
function status(message, kind='') {el('status').textContent=message;el('status').className=kind;}
function controls(enabled) {el('camera').disabled=!enabled;el('album').disabled=!enabled;}
async function request(path, options={}) {
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),100000);
  try {
    const response=await fetch(path,{...options,cache:'no-store',signal:controller.signal,
      headers:{'Authorization':'Bearer '+token,...(options.headers||{})}});
    const data=await response.json();
    if(!response.ok) {const e=new Error(data.error||'请求失败');e.status=response.status;throw e;}
    return data;
  } finally {clearTimeout(timer);}
}
async function check() {
  if(busy||completed)return;
  if(!token){status('链接缺少访问令牌，请在电脑端重新扫码。','error');return;}
  busy=true;controls(false);el('retry').hidden=true;
  try {
    const data=await request('/api/session');
    status('已连接，可以拍照或选择图片。\n链接剩余约 '+Math.ceil(data.remaining/60)+' 分钟。');
    controls(true);
  } catch(e) {
    status(e.status ? e.message : '无法连接电脑。请检查局域网、网络地址和电脑防火墙；会话可能已结束，请重新扫码。','error');
    el('retry').hidden=false;
  } finally {busy=false;}
}
async function upload(file) {
  if(busy||completed||!file)return;
  if(file.size===0||file.size>30*1024*1024){status('照片为空或超过 30 MiB，请重新选择。','error');return;}
  selected=file;busy=true;controls(false);el('retry').hidden=true;
  status('正在上传，请保持页面打开…');
  try {
    await request('/api/upload',{method:'POST',body:file,
      headers:{'Content-Type':'application/octet-stream'}});
    completed=true;selected=null;
    status('电脑已接收。\n请回到电脑继续裁剪和提取签名；已有文件时请先确认使用此照片。','success');
  } catch(e) {
    status(e.status ? e.message : '未能确认上传结果，请先查看电脑是否已接收。若未收到，可重试；会话已关闭时请重新扫码。','error');
    controls(true);el('retry').hidden=false;
  } finally {busy=false;}
}
el('camera').onclick=()=>el('cameraFile').click();
el('album').onclick=()=>el('albumFile').click();
for(const id of ['cameraFile','albumFile'])el(id).onchange=()=>{
  const file=el(id).files[0];el(id).value='';upload(file);
};
el('retry').onclick=()=>selected?upload(selected):check();
check();
</script></body></html>'''
