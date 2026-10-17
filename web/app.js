'use strict';
const $ = id => document.getElementById(id);
const pages = {
  overview: ['实时观察','观察练级效率，保留每一段可信记录。'],
  history: ['历史回看','按日期与记录回看经验；缺失的时间不补造收益。'],
  combat: ['战斗明细','伤害、资源变化与怪物击杀，来自可见画面。'],
  quiz: ['三转答题','框选题目，核对参考答案；也可以手动查题。'],
  knowledge: ['攻略资料','阅读本地国服怀旧服资料，核对来源与版本。'],
  settings: ['识别与设置','调整识别、悬浮显示与提醒，处理需要核验的读数。']
};
const labels = {damage_dealt:'造成伤害',damage_taken:'受到伤害',hp_spent:'血量减少',mp_spent:'蓝量减少',hp_restored:'血量恢复',mp_restored:'蓝量恢复'};
const stateNames = {ready:'采样完整',warming:'采样中',stale:'等待读数',paused:'已暂停',restored:'已恢复 · 等待新读数',held:'保留可信读数',retry:'正在核验',unavailable:'等待读数',live:'实时读数',interrupted:'采样中断'};
const number = value => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString('zh-CN',{maximumFractionDigits:0});
const decimal = value => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString('zh-CN',{maximumFractionDigits:1});
const duration = value => {if(value == null) return '—';const s=Math.max(0,Math.floor(value));return [Math.floor(s/3600),Math.floor(s/60)%60,s%60].map(v=>String(v).padStart(2,'0')).join(':');};
const beijing = (stamp, date=false) => new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',...(date?{year:'numeric',month:'2-digit',day:'2-digit'}:{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})}).format(new Date(stamp*1000));
const today = () => beijing(Date.now()/1000,true);
const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let token = sessionStorage.getItem('maple-token') || '';
let connected = false, currentPage = 'overview', state = null, settingsLoaded = false;
let pollTimer = null, polling = false, generation = 0, historyGeneration = 0, combatGeneration = 0, liveGeneration = 0;
let historyOffset=0, historyCount=0, selectedDays=[], lastLiveFetch=0, lastLevel=null;
let capture=null, crop=null, purpose='exp', pointerStart=null, pipWindow=null;
const chartData=new Map();
function readPairingHash(){const fragment=new URLSearchParams(location.hash.slice(1)),value=fragment.get('token');if(!value||! /^[\w-]{32,128}$/.test(value))return false;token=value;generation++;settingsLoaded=false;lastLevel=null;sessionStorage.setItem('maple-token',token);history.replaceState(null,'',location.pathname+location.search);return true;}
readPairingHash();
const endpoint='http://127.0.0.1:17321';

function notice(message,error=false) {$('notice').textContent=message;$('notice').hidden=!message;$('notice').dataset.error=String(error);}
function updateConnection(value) {
  connected=value;
  $('connection').textContent=value?'已连接 · 本机计算':'未连接 · 采样状态未知';
  $('connection').dataset.connected=String(value);
  $('local-fallback').href=endpoint+'/'+(token?'#token='+encodeURIComponent(token):'');
  document.querySelectorAll('.connected').forEach(button=>button.disabled=!value||button.dataset.busy==='true');
  if(!value) {$('quality').textContent='已断线 · 以下为上次读数';$('updated').textContent='连接服务后恢复更新';}
  if(!value)document.querySelector('.efficiency').dataset.held='true';
  syncPip();
}
function syncPip(){
  if(!pipWindow||pipWindow.closed)return;
  const document=pipWindow.document;
  if(!document.querySelector('strong'))return;
  const age=state?.last_valid?Math.max(0,Math.floor(Date.now()/1000-state.last_valid)):null;
  document.querySelector('strong').textContent=$('primary-rate').textContent;
  document.querySelector('p').textContent=(!connected?'已断线 · 保留上次读数':$('quality').textContent)+(age!=null?' · '+age+' 秒前':'');
  document.querySelector('.efficiency').dataset.held=String(!connected||!state?.valid);
}
async function api(path, data, binary=false) {
  const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),15000);
  try {
    const response=await fetch(endpoint+path,{method:data===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,...(data===undefined?{}:{'Content-Type':'application/json'})},body:data===undefined?undefined:JSON.stringify(data),cache:'no-store',signal:controller.signal});
    if(!response.ok){let error;try{error=(await response.json()).error;}catch{}throw new Error(error||'本机服务返回错误 '+response.status);}
    return binary ? response.blob() : response.json();
  } catch(error) {
    if(error instanceof TypeError || error.name==='AbortError') throw new Error('无法连接本机服务。请启动 start-web.cmd 并允许浏览器访问本地网络；也可打开本机网页。');
    throw error;
  } finally {clearTimeout(timer);}
}
async function command(name,data={}) {const result=await api('/api/command/'+name,data);notice(result.status||'操作已完成');await poll();return result;}
async function guarded(button,task) {if(button?.disabled)return; if(button){button.disabled=true;button.dataset.busy='true';}try{await task();}catch(error){notice(error.message,true);}finally{if(button){delete button.dataset.busy;button.disabled=button.classList.contains('connected')&&!connected;}}}
function query(data){return new URLSearchParams(data).toString();}
function option(value,text){const item=document.createElement('option');item.value=value;item.textContent=text;return item;}
function emptyRow(tbody,cols,message){$(tbody).innerHTML=`<tr><td colspan="${cols}" class="empty">${esc(message)}</td></tr>`;}

function renderState(s) {
  state=s;
  $('recording').textContent=s.running?'暂停记录':'继续记录';
  $('capture-status').textContent=s.status||'等待采样';
  $('updated').textContent=s.last_valid ? '最后可信读数 '+beijing(s.last_valid) : '尚无可信读数';
  const display=s.display_window_rates||s.window_rates||{}, primary=display['60']||{};
  const lower=primary.has_gaps?'≥ ':'';
  $('primary-rate').textContent=lower+number(s.display_primary_per_minute);
  $('hour-rate').textContent=lower+number(s.display_primary_per_minute==null?null:s.display_primary_per_minute*60);
  $('trend-rate').textContent=number(s.display_trend_per_minute);
  $('quality').textContent=(stateNames[s.display_state]||s.display_state||stateNames[s.primary_state]||'等待采样')+(s.display_age_seconds!=null&&s.display_age_seconds>5?' · '+Math.floor(s.display_age_seconds)+' 秒前':'');
  document.querySelector('.efficiency').dataset.held=String(!s.valid);
  $('coverage').value=primary.observed_seconds||0;
  $('coverage-text').textContent=decimal(primary.observed_seconds||0)+' / 60 秒有效观察';
  $('total').textContent=number(s.total);$('elapsed').textContent=duration(s.elapsed);$('average').textContent=number(s.per_minute);$('level').textContent=s.level==null?'—':'Lv. '+s.level;
  const current=s.display_current_exp ?? (s.valid?s.current:null), percent=s.display_current_percent ?? (s.valid?s.percent:null);
  $('current-exp').textContent=number(current);$('percent').textContent=percent==null?'—':Number(percent).toFixed(2)+'%';
  $('percent-rate').textContent=[s.percent_per_minute,s.percent_per_hour].map(v=>v==null?'—':Number(v).toFixed(2)+'%').join(' / ');
  const eta=s.level_eta_minutes;
  $('eta').textContent=eta==null?'—':decimal(eta)+' 分钟'+(s.valid?'':' · 保留估计');
  const v=s.combat?.vitals||{};$('vitals').textContent=(v.hp?v.hp.map(number).join(' / '):'—')+' · '+(v.mp?v.mp.map(number).join(' / '):'—');
  const f=s.flower_buff||{};$('flower').textContent=f.label||f.text||f.status||'等待花 Buff';
  $('flower-detail').textContent=f.audio_error||((f.enabled?'提醒开启':'提醒关闭')+' · '+(f.music?'音乐开启':'音乐关闭'));
  $('windows').innerHTML=[60,120,300,600,1800].map(seconds=>{const w=display[String(seconds)]||{};return `<div class="window"><span>${seconds/60} 分钟 · EXP / 分</span><strong>${w.has_gaps?'≥ ':''}${number(w.value)}</strong><small>${decimal(w.observed_seconds||0)} / ${seconds} 秒 · ${esc(stateNames[w.state]||'等待采样')}</small></div>`;}).join('');
  $('combat-totals').innerHTML=Object.entries(labels).map(([k,name])=>`<div><dt>${name}</dt><dd>${number(s.combat?.totals?.[k])}</dd></div>`).join('');
  const q=s.quiz||{};$('quiz-toggle').textContent=q.running?'暂停读题':'开始读题';
  $('quiz-status').textContent=({matched:'找到参考答案',uncertain:'需要核对',no_match:'未匹配到题目',waiting:'等待读题',paused:'读题已暂停',error:'暂时无法读题',ready:'选区已准备',idle:'尚未开始识别'})[q.status]||q.status||'尚未开始识别';
  // Only a confirmed match may promote an answer into the automatic result.
  $('quiz-question').textContent=q.entry?.question||'';
  $('quiz-answer').textContent=q.status==='matched'&&q.entry?q.entry.answer:'—';
  $('quiz-reason').textContent=q.reason||'请按答案文字核对游戏中的选项，不推断选项序号。';$('quiz-raw').textContent=q.raw||'';
  $('review-status').textContent=s.startup_review_error||s.review?.summary||'暂无后台评审事件';
  $('diagnostics').textContent=JSON.stringify({capture:s.capture,diagnostics:s.diagnostics,history_path:s.history_path,history_error:s.history_error,session_id:s.session_id,review:s.review},null,2);
  if(!settingsLoaded){$('cap').value=s.settings?.cap||'';$('opacity').value=Math.round((s.settings?.opacity||.58)*100);$('opacity-value').value=$('opacity').value+'%';$('flower-enabled').checked=f.enabled!==false;$('music').checked=f.music!==false;$('ahead').value=s.settings?.flower_ahead||15;settingsLoaded=true;}
  if(lastLevel!==null&&s.confirmed_levels>lastLevel){$('level-toast').textContent='升级了！'+(s.level?'Lv. '+s.level:'');$('level-toast').hidden=false;setTimeout(()=>$('level-toast').hidden=true,4500);}lastLevel=s.confirmed_levels;
  syncPip();
}

async function poll() {
  if(!token||polling)return;
  const version=generation;polling=true;
  try {
    const s=await api('/api/state');if(version!==generation)return;
    if(!s.updated||Date.now()/1000-s.updated>8)throw new Error('服务状态已停止更新，请检查本机程序。');
    const first=!connected;updateConnection(true);$('connect-panel').hidden=true;renderState(s);
    if(first){notice('已连接，本机历史与游戏画面不会上传。');await loadPage();}
    if(currentPage==='overview'&&Date.now()-lastLiveFetch>5000){lastLiveFetch=Date.now();await loadLive();}
  } catch(error) {if(version===generation){updateConnection(false);notice(error.message,true);}}
  finally {polling=false;}
}
function schedulePoll(){clearTimeout(pollTimer);pollTimer=setTimeout(async()=>{await poll();schedulePoll();},document.hidden?5000:1000);}

function drawChart(id,rows,bars=false) {
  chartData.set(id,{rows,bars});
  const target=$(id), valid=rows.filter(r=>r.value!=null&&Number.isFinite(Number(r.value)));
  if(!valid.length){target.innerHTML='<div class="empty">此范围尚无有效记录</div>';return;}
  const width=Math.max(320,target.clientWidth||600), height=target.clientHeight||200, left=58,right=width-18,top=24,bottom=height-30;
  const max=Math.max(1,...valid.map(r=>r.value))*1.1, start=rows[0].timestamp,end=rows[rows.length-1].timestamp;
  const x=(r,i)=>bars?left+(r.timestamp-start+30)/Math.max(60,end-start+60)*(right-left):left+(r.timestamp-start)/Math.max(1,end-start)*(right-left);
  const y=value=>bottom-(value/max)*(bottom-top);
  let svg=`<svg viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">`;
  [0,.5,1].forEach(f=>{let yy=y(max*f);svg+=`<line x1="${left}" x2="${right}" y1="${yy}" y2="${yy}" stroke="#304861"/><text x="${left-8}" y="${yy+4}" text-anchor="end">${number(max*f)}</text>`;});
  let previous=null;
  rows.forEach((r,i)=>{if(r.value==null){previous=null;return;}const xx=x(r,i),yy=y(r.value),gap=!!r.has_gaps;
    if(bars){const bw=Math.max(.5,Math.min(36,(right-left)*60/Math.max(60,end-start+60)*.8));svg+=`<rect x="${xx-bw/2}" y="${Math.min(bottom-1,yy)}" width="${bw}" height="${Math.max(1,bottom-yy)}" fill="#84b4de"><title>${esc(beijing(r.timestamp))} · ${number(r.value)}</title></rect>`;}
    else{if(previous&&JSON.stringify(r.segment)===JSON.stringify(previous.r.segment))svg+=`<line x1="${previous.x}" y1="${previous.y}" x2="${xx}" y2="${yy}" stroke="${gap?'#f2c572':'#84c5ff'}" stroke-width="2" ${gap?'stroke-dasharray="5 4"':''}/>`;svg+=`<circle cx="${xx}" cy="${yy}" r="2" fill="${gap?'#f2c572':'#84c5ff'}"><title>${esc(beijing(r.timestamp))} · ${gap?'≥ ':''}${number(r.value)}</title></circle>`;previous={r,x:xx,y:yy};}
  });
  svg+=`<text x="${left}" y="${height-8}">${esc(beijing(start))}</text><text x="${right}" y="${height-8}" text-anchor="end">${esc(beijing(end))}</text>`;
  if(rows.some(r=>r.has_gaps))svg+=`<text x="${right}" y="14" text-anchor="end">虚线 ≥ 下界 · 升级尾段未计入</text>`;
  target.innerHTML=svg+'</svg>';
}
async function loadLive(){if(!state||!connected)return;const version=++liveGeneration,connectionGeneration=generation,day=today(),session=state.session_id||'',window=$('live-window').value;const data=await api('/api/history?'+query({action:'live',day,session,window}));if(version!==liveGeneration||connectionGeneration!==generation||!connected||currentPage!=='overview'||window!==$('live-window').value||session!==(state?.session_id||''))return;drawChart('live-chart',data.plot);drawChart('live-minutes',data.minutes.slice(-30).map(r=>({...r,value:r.gain})),true);drawChart('today-chart',data.today_minutes.map(r=>({...r,value:r.gain})),true);$('today-total').textContent=number(data.today_gain)+' EXP';}
async function loadDays(){const result=await api('/api/history?action=days');selectedDays=result.days;const select=$('history-day'),old=select.value;select.replaceChildren(...result.days.map(d=>option(d.day,`${d.day} · ${number(d.gain)} EXP`)));if(result.days.some(d=>d.day===old))select.value=old;if(!result.days.length)select.append(option('','尚无历史记录'));}
async function loadHistory(resetSessions=false){
  const day=$('history-day').value;if(!day){emptyRow('history-rows',7,'尚无历史，开始记录后在这里回看');return;}
  const version=++historyGeneration, session=resetSessions?'':$('history-session').value;
  const data=await api('/api/history?'+query({day,session,window:$('history-window').value,offset:historyOffset}));if(version!==historyGeneration)return;
  if(resetSessions){$('history-session').replaceChildren(option('','全部记录'),...data.sessions.map(s=>option(s.id,`${beijing(s.first)} — ${beijing(s.last)} · ${number(s.gain)} EXP`)));}
  historyCount=data.total_rows;const summary=selectedDays.find(d=>d.day===day);
  $('day-summary').textContent=`${day} · ${number(data.gain)} EXP · ${number(data.total_rows)} 条采样`+(session?'':` · 有效计时 ${duration(summary?.seconds)}${summary?.seconds_estimated?'（历史重建）':''}`);
  drawChart('history-chart',data.plot);drawChart('history-minutes',data.minutes.map(r=>({...r,value:r.gain})),true);
  $('history-page').textContent=`${data.total_rows?historyOffset+1:0}–${Math.min(historyOffset+200,data.total_rows)} / ${data.total_rows}`;$('history-prev').disabled=historyOffset===0;$('history-next').disabled=historyOffset+200>=historyCount;
  $('history-rows').innerHTML=data.rows.map(r=>`<tr><td>${beijing(r.timestamp)}</td><td>${number(r.current_exp)}</td><td>${number(r.gain)}</td><td>${number(r.total)}</td><td>${decimal(r.elapsed)}</td><td>${r.window_gap_mask&1?'≥ ':''}${number(r.rolling60)}</td><td>${esc(r.status)}</td></tr>`).join('');
  if(!data.rows.length)emptyRow('history-rows',7,'该范围没有采样记录');
}
async function loadCombat(){const version=++combatGeneration;const data=await api('/api/history?'+query({action:'combat',day:$('combat-day').value||today()}));if(version!==combatGeneration)return;const day=$('combat-day').value||today(),totals=data.days[day];$('combat-daily').innerHTML=Object.entries(labels).map(([k,n])=>`<div><dt>${n}</dt><dd>${number(totals?.[k])}</dd></div>`).join('');$('kills').innerHTML=data.kills.length?data.kills.map(k=>`<span>${esc(k.name)} <b>${number(k.count)}</b></span>`).join(''):'<p class="empty">该日尚无确认击杀</p>';$('combat-rows').innerHTML=data.events.map(e=>`<tr><td>${beijing(e.timestamp)}</td><td>${labels[e.kind]||esc(e.kind)}</td><td>${number(e.amount)}</td><td>${esc(e.detail)}</td></tr>`).join('');if(!data.events.length)emptyRow('combat-rows',4,'该日尚无战斗事件');}
async function loadQuiz(){const data=await api('/api/quiz-bank?'+query({q:$('quiz-search').value}));$('quiz-entries').innerHTML=data.entries.map(e=>`<article><h3>${esc(e.question)}</h3><p>${esc(e.answer)}</p><small>${esc(e.source||'社区参考资料')}${e.requires_answer_visible?' · 自动匹配还需答案出现在画面中':''}</small></article>`).join('')||'<p class="empty">未找到相关题目，试试其他关键词。</p>';}
async function loadKnowledge(){const data=await api('/api/knowledge?'+query({q:$('knowledge-search').value}));$('knowledge-list').replaceChildren();data.documents.forEach(doc=>{const button=document.createElement('button');button.textContent=doc.title;button.onclick=()=>guarded(button,async()=>{const result=await api('/api/knowledge?'+query({document:doc.document}));const pre=document.createElement('pre');pre.textContent=result.content;$('knowledge-content').replaceChildren(pre);});$('knowledge-list').append(button);});if(!data.documents.length)$('knowledge-list').innerHTML='<p class="empty">本机未找到匹配资料。知识库不会随公开网站分发。</p>';$('knowledge-claims').innerHTML=(data.claims||[]).map(c=>`<article><h3>${esc(c.subject)} · ${esc(c.predicate)}</h3><p>${esc(typeof c.value==='object'?JSON.stringify(c.value):c.value)} ${esc(c.unit||'')}</p><small>${esc(c.scope)} · ${esc(c.version)} · 证据质量 ${number(c.confidence)} / 100 · ${esc(c.status)} · 核验于 ${esc(c.last_verified)}</small><p>${esc(c.limitations)}</p><details><summary>证据、来源与适用范围</summary><pre>${esc(JSON.stringify(c,null,2))}</pre></details></article>`).join('');}
async function loadPage(){if(!connected)return;if(currentPage==='overview')await loadLive();if(currentPage==='history'){await loadDays();await loadHistory(true);}if(currentPage==='combat')await loadCombat();if(currentPage==='quiz')await loadQuiz();if(currentPage==='knowledge')await loadKnowledge();}
function navigate(name){if(!pages[name])return;currentPage=name;document.querySelectorAll('.page').forEach(page=>page.hidden=page.id!==name);document.querySelectorAll('[data-page]').forEach(button=>{if(button.dataset.page===name)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');});$('page-title').textContent=pages[name][0];$('page-description').textContent=pages[name][1];guarded(null,loadPage);}

async function download(kind){let action='csv',day=today(),session='';if(kind==='history'){day=$('history-day').value;session=$('history-session').value;}if(kind==='current'){session=state?.session_id||'';action='current_csv';}if(kind==='combat'||kind==='kills'){action=kind+'_csv';day=$('combat-day').value||today();}const blob=await api('/api/history?'+query({action,day,session}),undefined,true);const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`maple-${kind}-${day}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);notice('CSV 已导出，时间为北京时间。');}

function paintCrop(){const canvas=$('capture-canvas'),ctx=canvas.getContext('2d');ctx.clearRect(0,0,canvas.width,canvas.height);ctx.drawImage(capture.image,0,0);if(crop){ctx.fillStyle='#84c5ff22';ctx.fillRect(crop.left,crop.top,crop.width,crop.height);ctx.strokeStyle='#8cd8ff';ctx.lineWidth=Math.max(2,canvas.width/500);ctx.strokeRect(crop.left,crop.top,crop.width,crop.height);}}
async function captureGame(target){purpose=target;notice('3 秒后截取游戏画面，请切回游戏。截图完成后返回网页框选。');const result=await api('/api/screenshot',{delay:3});const image=new Image();image.src=result.image;await image.decode();capture={...result,image};crop=null;const canvas=$('capture-canvas');canvas.width=result.width;canvas.height=result.height;paintCrop();$('capture-help').textContent=target==='quiz'?'框选完整题干和所有选项；请勿移动游戏窗口。':'仅框选完整经验数字；请勿移动游戏窗口。';$('capture-dialog').showModal();}
function cropInputs(){if(!crop)return;for(const k of ['left','top','width','height'])$('region-'+k).value=crop[k];}
const canvas=$('capture-canvas');
const point=event=>{const r=canvas.getBoundingClientRect();return {x:Math.max(0,Math.min(canvas.width,Math.round((event.clientX-r.left)/r.width*canvas.width))),y:Math.max(0,Math.min(canvas.height,Math.round((event.clientY-r.top)/r.height*canvas.height)))};};
canvas.addEventListener('pointerdown',event=>{pointerStart=point(event);canvas.setPointerCapture(event.pointerId);});
canvas.addEventListener('pointermove',event=>{if(!pointerStart)return;const p=point(event);crop={left:Math.min(p.x,pointerStart.x),top:Math.min(p.y,pointerStart.y),width:Math.abs(p.x-pointerStart.x),height:Math.abs(p.y-pointerStart.y)};cropInputs();paintCrop();});
canvas.addEventListener('pointerup',()=>{pointerStart=null;});canvas.addEventListener('pointercancel',()=>{pointerStart=null;});
['left','top','width','height'].forEach(k=>$('region-'+k).addEventListener('input',()=>{crop=Object.fromEntries(['left','top','width','height'].map(k=>[k,Number($('region-'+k).value)]));paintCrop();}));
$('save-region').onclick=()=>guarded($('save-region'),async()=>{crop=Object.fromEntries(['left','top','width','height'].map(k=>[k,Number($('region-'+k).value)]));if(crop.width<40||crop.height<20)throw new Error('选区至少为 40 × 20 像素');await command('region',{...crop,purpose,ticket:capture.ticket});$('capture-dialog').close();notice('选区已保存，请开始记录或读题。');});

document.querySelectorAll('[data-page]').forEach(button=>button.onclick=()=>navigate(button.dataset.page));
document.querySelectorAll('[data-command]').forEach(button=>button.onclick=()=>guarded(button,()=>command(button.dataset.command,{target:button.dataset.target})));
document.querySelectorAll('[data-export]').forEach(button=>button.onclick=()=>guarded(button,()=>download(button.dataset.export)));
document.querySelectorAll('[data-capture]').forEach(button=>button.onclick=()=>guarded(button,()=>captureGame(button.dataset.capture)));
document.querySelectorAll('[data-confirm]').forEach(button=>button.onclick=()=>{const text={level:'已核对游戏画面，确认刚刚升了一级？',rebase:'跳过当前经验差值并建立新基线？这一段差值将不会计入。',reset:'结束本次统计并新建记录？历史仍会保留，当前分均采样将清空。'}[button.dataset.confirm];if(confirm(text))guarded(button,()=>command(button.dataset.confirm,{confirmed:true}));});
$('recording').onclick=()=>guarded($('recording'),()=>command('recording',{enabled:!state?.running}));
$('quiz-toggle').onclick=()=>guarded($('quiz-toggle'),()=>command('quiz',{enabled:!state?.quiz?.running}));
$('quiz-pdf').onclick=()=>guarded($('quiz-pdf'),async()=>{const blob=await api('/api/quiz-pdf',undefined,true),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='冒险岛三转答题速查.pdf';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
$('show-connect').onclick=()=>{$('connect-panel').hidden=!$('connect-panel').hidden;if(!$('connect-panel').hidden)$('connection-code').focus();};
$('connect-form').onsubmit=event=>{event.preventDefault();let value=$('connection-code').value.trim();if(value.includes('#'))value=new URLSearchParams(value.split('#')[1]).get('token')||'';if(!/^[\w-]{32,128}$/.test(value)){notice('连接码格式不正确，请使用本机启动入口生成的连接地址。',true);return;}generation++;token=value;sessionStorage.setItem('maple-token',token);$('connection-code').value='';settingsLoaded=false;guarded(null,poll);};
$('disconnect').onclick=()=>{generation++;token='';sessionStorage.removeItem('maple-token');updateConnection(false);$('connect-panel').hidden=false;notice('网页已断开；本机程序会继续按原状态运行。');};
$('settings-form').onsubmit=event=>{event.preventDefault();guarded(event.submitter,()=>command('settings',{cap:$('cap').value,opacity:Number($('opacity').value)/100,flower_enabled:$('flower-enabled').checked,music:$('music').checked,ahead:Number($('ahead').value)}));};
$('opacity').oninput=()=>$('opacity-value').value=$('opacity').value+'%';
$('history-day').onchange=()=>{historyOffset=0;guarded(null,()=>loadHistory(true));};
$('history-session').onchange=$('history-window').onchange=()=>{historyOffset=0;guarded(null,()=>loadHistory());};
$('history-refresh').onclick=()=>guarded($('history-refresh'),async()=>{await loadDays();await loadHistory(true);});
$('history-prev').onclick=()=>{historyOffset=Math.max(0,historyOffset-200);guarded(null,()=>loadHistory());};
$('history-next').onclick=()=>{historyOffset+=200;guarded(null,()=>loadHistory());};
$('combat-refresh').onclick=()=>guarded($('combat-refresh'),loadCombat);
$('combat-day').value=today();$('combat-day').onchange=()=>guarded(null,loadCombat);
$('quiz-search-form').onsubmit=event=>{event.preventDefault();guarded(event.submitter,loadQuiz);};
$('knowledge-search-form').onsubmit=event=>{event.preventDefault();guarded(event.submitter,loadKnowledge);};
$('live-window').onchange=()=>guarded(null,loadLive);
const chartResize=new ResizeObserver(entries=>{for(const entry of entries){const saved=chartData.get(entry.target.id);if(saved&&entry.contentRect.width>0)drawChart(entry.target.id,saved.rows,saved.bars);}});
document.querySelectorAll('.chart').forEach(element=>chartResize.observe(element));
$('pip').onclick=()=>guarded($('pip'),async()=>{if(!window.documentPictureInPicture){notice('此浏览器不支持网页置顶小窗，请使用桌面悬浮球。');return;}pipWindow=await window.documentPictureInPicture.requestWindow({width:330,height:210});const link=pipWindow.document.createElement('link');link.rel='stylesheet';link.href=new URL('./style.css',location.href).href;pipWindow.document.head.append(link);pipWindow.document.body.innerHTML='<section class="efficiency"><h2>经验观察器 · EXP / 分</h2><div class="rate"><strong>—</strong></div><p>等待读数</p></section>';syncPip();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden){guarded(null,poll);schedulePoll();}});
window.addEventListener('hashchange',()=>{if(readPairingHash())guarded(null,poll);else navigate(location.hash.slice(1));});
updateConnection(false);
['live-chart','live-minutes','today-chart','history-chart','history-minutes'].forEach(id=>drawChart(id,[]));
$('windows').innerHTML=[1,2,5,10,30].map(m=>`<div class="window"><span>${m} 分钟 · EXP / 分</span><strong>—</strong><small>等待连接</small></div>`).join('');
emptyRow('history-rows',7,'连接后载入已保存记录');emptyRow('combat-rows',4,'连接后查看战斗事件');
if(token)guarded(null,poll);schedulePoll();
