"""HTML/JS renderer for the HUD v3 rules. Draws the dict from ``viewmodel.build_model`` and nothing else.

One list-row component, one button style, one confirm dialog, a toast; colour only for status. The page
holds no state of its own: every tap is posted as ``{action, args}`` to the shell and the next model is
pushed back through ``window.render(model)``. All text is inserted with ``textContent`` (app names, source
names and similar are user data), never as HTML.
"""
from __future__ import annotations

import json
from typing import Optional

HUD_WIDTH = 360
HUD_HEIGHT = 600

_CSS = """
:root{--bg:#F5F5F3;--card:#FFFFFF;--line:#E6E6E2;--sep:#EFEFEB;--text:#1C1C1E;--sub:#85857F;--ok:#5A9E78;--amber:#C88A2A;--red:#C4473A;--grey:#B4B4AE;--blue:#2F6FDB;--amber-text:#94661C;--red-text:#9A3A2E;--track:#D9D9D4}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--text);font-family:-apple-system,'PingFang SC','Helvetica Neue',sans-serif;-webkit-font-smoothing:antialiased;-webkit-user-select:none;user-select:none}
button{font-family:inherit;cursor:pointer}
#app{position:relative;width:360px;height:600px;overflow:hidden;display:flex;flex-direction:column;background:var(--bg)}
#statusbar{height:56px;flex-shrink:0;padding:0 14px;background:var(--card);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:10px}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot.sm{width:7px;height:7px}
.grow{flex:1;min-width:0;display:flex;flex-direction:column;gap:1px}
#st-text{font-size:14px;font-weight:600}
#st-sub{font-size:11px;color:var(--sub)}
#recheck{height:30px;padding:0 8px;border-radius:8px;border:1px solid #D6D6D1;background:var(--card);font-size:12px;font-weight:500;color:var(--text);flex-shrink:0}
#recheck:hover{background:#F4F4F1}
#pause{height:30px;padding:0 12px;border-radius:8px;border:1px solid #D6D6D1;background:var(--card);font-size:12px;font-weight:600;color:var(--text)}
#titlebar{height:54px;flex-shrink:0;padding:0 8px;display:flex;flex-direction:column;align-items:stretch}
.title-row{height:33px;flex-shrink:0;display:flex;align-items:center;position:relative}
#back{height:32px;padding:0 6px;border:0;background:transparent;font-size:13px;color:var(--blue);display:flex;align-items:center;gap:2px;position:relative;z-index:1}
#title{position:absolute;left:0;right:0;text-align:center;font-size:13px;font-weight:600;pointer-events:none}
#model-updated{height:14px;text-align:center;font-size:10px;line-height:14px;color:var(--sub);font-variant-numeric:tabular-nums}
#scroll{flex:1;min-height:0;overflow-y:auto;padding:0 12px 14px;display:flex;flex-direction:column;gap:14px}
#scroll.dim{opacity:.5}
.sec{display:flex;flex-direction:column;gap:5px}
.hdr{padding:0 4px;font-size:11px;font-weight:600;color:var(--sub)}
.ftr{padding:0 4px;font-size:11px;color:var(--sub);line-height:1.5}
.stats{padding:4px 4px 0;display:flex;gap:22px}
.stat{display:flex;flex-direction:column;gap:1px}
.stat b{font-size:17px;font-weight:600;color:#3A3A37;font-variant-numeric:tabular-nums}
.stat span{font-size:11px;color:var(--sub)}
.card{background:var(--card);border-radius:10px;overflow:hidden}
.r{width:100%;min-height:46px;padding:7px 12px;border:0;border-top:1px solid var(--sep);background:transparent;display:flex;align-items:center;gap:10px;text-align:left;color:inherit}
.r:first-child{border-top:0}
.r.act:hover{background:#F4F4F1}
.r.act{cursor:pointer}
.r:not(.act){cursor:default}
.rt{flex:1;min-width:0;display:flex;flex-direction:column;gap:1px}
.rt .t{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rt .s{font-size:11px;color:var(--sub);line-height:1.4}
.val{font-size:12px;color:var(--sub);flex-shrink:0}
.sw{width:34px;height:20px;border-radius:10px;padding:2px;background:var(--track);display:flex;justify-content:flex-start;flex-shrink:0}
.sw.on{background:var(--blue);justify-content:flex-end}
.sw i{width:16px;height:16px;border-radius:8px;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.2)}
.btn{height:40px;border-radius:10px;border:0;background:var(--blue);color:#fff;font-size:13px;font-weight:600}
.btn.danger{background:var(--card);color:var(--red-text)}
pre.code{margin:0;padding:10px 12px;background:var(--card);border-radius:10px;font:11px/1.5 ui-monospace,'SF Mono',Menlo,monospace;color:#3A3A37;white-space:pre-wrap;word-break:break-all;-webkit-user-select:text;user-select:text;max-height:220px;overflow:auto}
#toast{position:absolute;left:50%;bottom:62px;transform:translateX(-50%);padding:7px 14px;border-radius:16px;background:#2C2C2A;color:#fff;font-size:12px;white-space:nowrap;max-width:320px;overflow:hidden;text-overflow:ellipsis;display:none}
#tabs{height:50px;flex-shrink:0;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));background:var(--card);border-top:1px solid var(--line)}
.tab{border:0;background:transparent;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;font-size:12px;color:var(--sub)}
.tab.cur{font-weight:600;color:var(--text)}
.tab .l{display:flex;align-items:center;gap:4px}
.tab .bar{width:18px;height:2px;border-radius:1px;background:transparent}
.tab.cur .bar{background:var(--blue)}
.tab .bd{width:6px;height:6px;border-radius:50%}
#veil{position:absolute;inset:0;background:rgba(20,20,19,.28);display:none;align-items:center;justify-content:center;padding:24px}
#dlg{width:100%;background:var(--card);border-radius:12px;padding:16px;display:flex;flex-direction:column;gap:8px}
#dlg h3{margin:0;font-size:14px;font-weight:650}
#dlg p{margin:0;font-size:12px;color:#5C5C58;line-height:1.5}
#dlg .row{display:flex;gap:8px;padding-top:6px}
#dlg button{flex:1;height:36px;border-radius:8px;border:1px solid #D6D6D1;background:var(--card);font-size:13px}
#dlg button.ok{border:0;background:var(--red);color:#fff;font-weight:600}
"""

_JS = r"""
(function(){
  var TONE={ok:'var(--ok)',amber:'var(--amber)',red:'var(--red)',grey:'var(--grey)'};
  var TEXT={amber:'var(--amber-text)',red:'var(--red-text)',blue:'var(--blue)'};
  var NS='http://www.w3.org/2000/svg';
  var $=function(id){return document.getElementById(id)};
  var lastToast=null, lastView='', toastTimer=null, modelBuiltAt=null;

  function updateAge(){
    var label=$('model-updated');if(!label)return;
    var ts=modelBuiltAt?Date.parse(modelBuiltAt):NaN;
    label.textContent=Number.isFinite(ts)?'数据更新于 '+Math.max(0,Math.floor((Date.now()-ts)/1000))+' 秒前':'数据更新尚未完成';
  }

  function post(action,args){
    var msg={action:action,args:args||{}};
    var h=window.webkit&&window.webkit.messageHandlers&&window.webkit.messageHandlers.penpal;
    if(h){h.postMessage(msg)}else if(window.__post){window.__post(msg)}
  }
  function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
  function svg(w,h,d,stroke,sw){
    var s=document.createElementNS(NS,'svg');s.setAttribute('width',w);s.setAttribute('height',h);s.setAttribute('viewBox','0 0 '+w+' '+h);s.setAttribute('aria-hidden','true');s.style.flexShrink='0';
    var p=document.createElementNS(NS,'path');p.setAttribute('d',d);p.setAttribute('fill','none');p.setAttribute('stroke',stroke);p.setAttribute('stroke-width',sw);p.setAttribute('stroke-linecap','round');p.setAttribute('stroke-linejoin','round');s.appendChild(p);return s;
  }
  function dot(tone,small){var d=el('span','dot'+(small?' sm':''));d.style.background=TONE[tone]||TONE.grey;return d}

  function row(r){
    var act=!!r.action;
    var b=el(act?'button':'div','r'+(act?' act':''));
    if(act){b.type='button';b.addEventListener('click',function(){post(r.action,r.args)})}
    var tr=r.trail;
    if(tr&&tr.type==='switch'){b.setAttribute('role','switch');b.setAttribute('aria-checked',String(tr.on))}
    else if(tr&&tr.type==='check'){b.setAttribute('role','radio');b.setAttribute('aria-checked',String(tr.on))}
    if(r.dot)b.appendChild(dot(r.dot,true));
    var t=el('span','rt');
    var tt=el('span','t',r.title);if(r.title_tone)tt.style.color=TEXT[r.title_tone];t.appendChild(tt);
    if(r.sub){var s=el('span','s',r.sub);if(r.sub_tone)s.style.color=TEXT[r.sub_tone];t.appendChild(s)}
    b.appendChild(t);
    if(r.value){var v=el('span','val',r.value.text);if(r.value.tone)v.style.color=TEXT[r.value.tone];b.appendChild(v)}
    if(tr){
      if(tr.type==='switch'){var sw=el('span','sw'+(tr.on?' on':''));sw.appendChild(el('i'));b.appendChild(sw)}
      else if(tr.type==='check'){var c=svg(14,14,'M3 7.5l2.5 2.5L11 4.5','#2F6FDB',1.8);c.style.opacity=tr.on?'1':'0';b.appendChild(c)}
      else if(tr.type==='nav'){b.appendChild(svg(12,12,'M4.5 2.5L8 6l-3.5 3.5','#B4B4AE',1.6))}
    }
    return b;
  }

  function section(s){
    var w=el('div','sec');
    if(s.header)w.appendChild(el('div','hdr',s.header));
    if(s.kind==='stats'){
      var st=el('div','stats');
      s.stats.forEach(function(x){var d=el('div','stat');d.appendChild(el('b',null,x.v));d.appendChild(el('span',null,x.k));st.appendChild(d)});
      w.appendChild(st);
    }else if(s.kind==='list'){
      var card=el('div','card');s.rows.forEach(function(r){card.appendChild(row(r))});w.appendChild(card);
    }else if(s.kind==='code'){
      w.appendChild(el('pre','code',s.text));
    }else if(s.kind==='button'){
      var b=el('button','btn'+(s.tone==='danger'?' danger':''),s.label);b.type='button';
      b.addEventListener('click',function(){post(s.action,s.args)});w.appendChild(b);
    }
    if(s.footer)w.appendChild(el('div','ftr',s.footer));
    return w;
  }

  window.render=function(m){
    var bar=m.status;
    var sb=$('statusbar');sb.textContent='';
    sb.appendChild(dot(bar.tone,false));
    var g=el('div','grow');var t1=el('span',null,bar.text);t1.id='st-text';var t2=el('span',null,bar.sub);t2.id='st-sub';g.appendChild(t1);g.appendChild(t2);sb.appendChild(g);
    var rb=el('button',null,'重新检测');rb.id='recheck';rb.type='button';rb.addEventListener('click',function(){post('health.recheck')});sb.appendChild(rb);
    var pb=el('button',null,m.pause.label);pb.id='pause';pb.type='button';pb.addEventListener('click',function(){post(m.pause.action)});sb.appendChild(pb);

    modelBuiltAt=m.model_built_at||null;
    var tb=$('titlebar');tb.textContent='';
    var tr=el('div','title-row');
    if(m.back){
      var bk=el('button',null);bk.id='back';bk.type='button';bk.appendChild(svg(12,12,'M8 2L4 6l4 4','#2F6FDB',1.6));bk.appendChild(document.createTextNode(m.back.label));
      bk.addEventListener('click',function(){post(m.back.action)});tr.appendChild(bk);
    }
    tr.appendChild(el('div',null,m.title)).id='title';
    tb.appendChild(tr);
    var age=el('div',null);age.id='model-updated';age.setAttribute('aria-live','polite');tb.appendChild(age);updateAge();

    var sc=$('scroll');var key=m.view.tab+'|'+m.view.page;var keep=(key===lastView)?sc.scrollTop:0;lastView=key;
    sc.textContent='';sc.className=m.dim?'dim':'';
    m.sections.forEach(function(s){sc.appendChild(section(s))});
    sc.scrollTop=keep;

    var tabs=$('tabs');tabs.textContent='';
    m.tabs.forEach(function(x){
      var b=el('button','tab'+(x.current?' cur':''));b.type='button';if(x.current)b.setAttribute('aria-current','page');
      var l=el('span','l',x.label);if(x.badge){var d=el('span','bd');d.style.background=TONE[x.badge];l.appendChild(d)}
      b.appendChild(l);b.appendChild(el('span','bar'));
      b.addEventListener('click',function(){post('nav.tab',{tab:x.id})});tabs.appendChild(b);
    });

    var veil=$('veil');
    if(m.confirm){
      $('dlg-title').textContent=m.confirm.title;$('dlg-body').textContent=m.confirm.body;$('dlg-ok').textContent=m.confirm.ok;
      $('dlg').setAttribute('aria-label',m.confirm.title);veil.style.display='flex';
    }else{veil.style.display='none'}

    var ts=$('toast');
    if(m.toast&&m.toast.id!==lastToast){
      lastToast=m.toast.id;ts.textContent=m.toast.text;ts.style.display='block';
      clearTimeout(toastTimer);toastTimer=setTimeout(function(){ts.style.display='none'},2400);
    }
  };
  $('dlg-cancel').addEventListener('click',function(){post('confirm.cancel')});
  $('dlg-ok').addEventListener('click',function(){post('confirm.ok')});
  setInterval(updateAge,1000);
  window.addEventListener('load',function(){post('ui.ready')});
})();
"""

_BODY = """
<div id="app">
  <div id="statusbar"></div>
  <div id="titlebar"></div>
  <div id="scroll"></div>
  <div id="toast" role="status"></div>
  <div id="tabs"></div>
  <div id="veil"><div id="dlg" role="dialog"><h3 id="dlg-title"></h3><p id="dlg-body"></p>
    <div class="row"><button id="dlg-cancel" type="button">取消</button><button id="dlg-ok" class="ok" type="button">确认</button></div></div></div>
</div>
"""


def _json_for_script(model: dict) -> str:
    return json.dumps(model, ensure_ascii=False).replace("</", "<\\/")


def build_html(initial_model: Optional[dict] = None) -> str:
    """The whole page. With ``initial_model`` the page renders it on load (used by the shell and by static
    previews); the shell then pushes later models through ``window.render``."""
    boot = f"window.render({_json_for_script(initial_model)});" if initial_model is not None else ""
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=360,initial-scale=1">'
        f"<title>Penpal</title><style>{_CSS}</style></head><body>{_BODY}"
        f"<script>{_JS}</script><script>{boot}</script></body></html>"
    )
