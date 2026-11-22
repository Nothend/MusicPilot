// 页面冒烟测试：用 jsdom 加载真实页面，逐个点开「推荐」的子页面并点按钮，捕获所有脚本错误。
// 用法见同目录 README.md：node ui_test.js <端口>
const { JSDOM, VirtualConsole } = require('jsdom');
const PORT = process.argv[2], BASE = `http://127.0.0.1:${PORT}`;
const errors = [], logs = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errors.push('jsdomError: ' + (e.detail || e.message)));
vc.on('error', e => errors.push('console.error: ' + e));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const check = (name, cond, extra) => { logs.push(`${cond ? '✔' : '✘'} ${name}${extra ? ' — ' + extra : ''}`); if (!cond) errors.push('断言失败: ' + name); };

(async () => {
  const dom = await JSDOM.fromURL(BASE + '/', {
    runScripts: 'dangerously', resources: 'usable', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = (u, o) => fetch(new URL(u, BASE), o);
      w.addEventListener('error', e => errors.push('window.onerror: ' + e.message));
      w.addEventListener('unhandledrejection', e => errors.push('unhandledrejection: ' + e.reason));
      w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
      w.HTMLDialogElement.prototype.close = function (v) { this.removeAttribute('open'); if (v !== undefined) this.returnValue = v; this.dispatchEvent(new w.Event('close')); };
    },
  });
  const w = dom.window, d = w.document, $ = id => d.getElementById(id), txt = id => ($(id).textContent || '').replace(/\s+/g, ' ');
  await sleep(1500);                                                   // 页面加载 + loadState/initData
  check('登录态下主界面可见', !$('main').classList.contains('hide'));
  check('导航里有「推荐」标签', !!d.querySelector('#nav button[data-tab="rec"]'));

  // ---- 页面结构：推荐在最前并作为默认页；下载与定时任务合并；待处理卡片与角标
  const navTabs = [...d.querySelectorAll('#nav button')].map(b => b.dataset.tab);
  check('导航顺序：推荐 / 网易云下载 / 任务记录 / 设置', navTabs.join(',') === 'rec,dl,jobs,set', navTabs.join(','));
  check('推荐联动已启用时，默认打开「推荐」', $('p-rec').classList.contains('on') && !$('p-dl').classList.contains('on'));
  await sleep(600);
  check('「需要你处理」卡片列出待选择候选', !$('todoCard').classList.contains('hide') && txt('todoList').includes('待选择'), txt('todoList'));
  check('「推荐」标签上有待处理角标', /\d/.test(txt('navRec')), txt('navRec'));
  await w.eval("todoGo('multiple')"); await sleep(900);
  check('点「去选择」直达列表并筛到「待选择」', !$('rv-list').classList.contains('hide') && txt('lstStates').includes('待选择') && $('lstItems').children.length >= 1 && !txt('lstStates').includes('下载失败'), txt('lstStates'));
  await w.eval("recShow('dash')");
  w.showTab('dl'); await sleep(300);
  check('「网易云下载」默认是手动下载', !$('dv-manual').classList.contains('hide') && $('dv-auto').classList.contains('hide'));
  d.querySelector('#dlNav button[data-v="auto"]').click(); await sleep(300);
  check('切到「定时任务」子页', $('dv-manual').classList.contains('hide') && !$('dv-auto').classList.contains('hide') && !!$('schedList'));
  w.showTab('auto'); await sleep(200);
  check('旧链接 #auto 仍能打开（定时任务）', $('p-dl').classList.contains('on') && !$('dv-auto').classList.contains('hide'));
  d.querySelector('#dlNav button[data-v="manual"]').click();

  // ---- 概览
  w.showTab('rec'); await sleep(900);
  check('概览子页可见', !$('rv-dash').classList.contains('hide') && $('rv-list').classList.contains('hide'));
  check('显示「已启用」', txt('dbEnabled').includes('已启用'), txt('dbEnabled'));
  check('统计卡片已渲染', $('dbTiles').children.length >= 10, `${$('dbTiles').children.length} 个`);
  const tiles = txt('dbTiles'); logs.push('   统计: ' + tiles.slice(0, 160));
  check('今日推荐=10（7+2+1）', /10今日推荐/.test(tiles));
  check('最新一份推荐含两类', txt('dbBatches').includes('Weekly Jams') && txt('dbBatches').includes('Daily Jams'), txt('dbBatches').slice(0, 120));
  check('状态徽标有「未找到」', txt('dbBatches').includes('未找到') && txt('dbBatches').includes('下载失败'));
  check('空闲状态 + 上次任务', txt('dbCurrent').includes('空闲') && txt('dbCurrent').includes('推荐同步'), txt('dbCurrent').slice(0, 80));
  check('显示计划', txt('dbNext').includes('每日同步'), txt('dbNext').slice(0, 80));

  // ---- 推荐歌单
  d.querySelector('#recNav button[data-v="list"]').click(); await sleep(900);
  check('列表子页可见', !$('rv-list').classList.contains('hide'));
  d.querySelector('#lstType button[data-v="weekly_jams"]').click(); await sleep(900);
  const rows = d.querySelectorAll('#lstItems .rit');
  check('Weekly Jams 列出 7 首', rows.length === 7, `${rows.length}`);
  const t = txt('lstItems');
  check('含各种状态', ['主库已有', '试听库', '未找到', '下载失败'].every(x => t.includes(x)), '');
  check('原因中文化', t.includes('网易云没有这首') && t.includes('只有 MP3'), '');
  check('有「重试」按钮', [...d.querySelectorAll('#lstItems button')].some(b => b.textContent === '重试'));
  check('试听库歌曲有「保留」按钮', [...d.querySelectorAll('#lstItems button')].some(b => b.textContent === '保留'));
  check('状态筛选条', txt('lstStates').includes('全部 7'), txt('lstStates'));
  w.setLstState('NOT_FOUND'); await sleep(700);
  check('按状态筛选后只剩 2 首（未找到+只有MP3）', d.querySelectorAll('#lstItems .rit').length === 2);
  w.setLstState(''); await sleep(500);
  check('列表页说明了该类推荐的含义', txt('lstDesc').includes('每周更新') && txt('lstDesc').includes('Weekly Jams'), txt('lstDesc'));
  check('类型按钮是 6 字以内的中文名', [...d.querySelectorAll('#lstType button')].every(b => /^[\u4e00-\u9fa5]{2,6}$/.test(b.textContent)), txt('lstType'));
  // ---- 疑似与主库重复（§17.16）
  const dupBtn = n => [...d.querySelectorAll('#lstItems button')].find(b => b.textContent === n);
  check('疑似重复提示显示两份文件信息', txt('lstItems').includes('疑似与主库重复') && txt('lstItems').includes('WAV · 3:51 · 30.0 MB') && txt('lstItems').includes('主库同名歌'), txt('lstItems').slice(0, 20));
  check('疑似重复有三个按钮', ['保留试听库这份', '保留主库这份', '两份都留'].every(n => !!dupBtn(n)));
  const nRuns = async () => (await (await fetch(BASE + '/api/lb/runs')).json()).runs.length;
  const r0 = await nRuns();
  dupBtn('保留试听库这份').click(); await sleep(300);
  check('保留试听库这份：先弹确认框并说明主库那份进回收站', $('recConfirm').hasAttribute('open') && txt('rcBody').includes('回收站') && txt('rcBody').includes('WAV'), txt('rcBody').slice(0, 80));
  $('recConfirm').close('no'); await sleep(500);
  check('取消确认后没有产生新任务', (await nRuns()) === r0, `${r0}→${await nRuns()}`);
  dupBtn('两份都留').click(); await sleep(900);
  check('点「两份都留」后提示消失', !txt('lstItems').includes('疑似与主库重复') && !dupBtn('两份都留'), txt('toast'));
  // 点「保留」
  const keepBtn = [...d.querySelectorAll('#lstItems button')].find(b => b.textContent === '保留');
  keepBtn.click(); await sleep(900);
  check('点「保留」后变成「取消保留」+ 保留标记', [...d.querySelectorAll('#lstItems button')].some(b => b.textContent === '取消保留') && txt('lstItems').includes('保留'));
  // 点「重试」
  const retryBtn = [...d.querySelectorAll('#lstItems button')].find(b => b.textContent === '重试');
  retryBtn.click(); await sleep(900);
  check('点「重试」后该条回到待处理', txt('lstItems').includes('待处理'));
  // 候选选择
  d.querySelector('#lstType button[data-v="weekly_exploration"]').click(); await sleep(900);
  check('待选择条目有「选择」按钮', [...d.querySelectorAll('#lstItems button')].some(b => b.textContent === '选择'));
  [...d.querySelectorAll('#lstItems button')].find(b => b.textContent === '选择').click(); await sleep(300);
  check('候选对话框列出 2 个候选', d.querySelectorAll('#candList .sched').length === 2 && $('candDlg').hasAttribute('open'), txt('candList').slice(0, 120));
  d.querySelector('#candList button').click(); await sleep(1000);
  check('选择试听库候选后该条变为「试听库」且不再有选择按钮', txt('lstItems').includes('试听库') && ![...d.querySelectorAll('#lstItems button')].some(b => b.textContent === '选择'), txt('lstItems').slice(0, 80));

  // ---- 任务
  d.querySelector('#nav button[data-tab="jobs"]').click(); await sleep(300);
  d.querySelector('#jKind button[data-v="lb"]').click(); await sleep(900);
  check('合并后：任务记录页里选「推荐联动」只显示推荐任务', d.getElementById('jobsSec').classList.contains('hide') && !d.getElementById('runsSec').classList.contains('hide'));
  check('任务列表有记录', d.querySelectorAll('#runsBox .run').length >= 1, `${d.querySelectorAll('#runsBox .run').length}`);
  d.querySelector('#runsBox .rh').click(); await sleep(900);
  const log = txt('rlog-' + d.querySelector('#runsBox .run').id.replace('run-', ''));
  check('展开后显示日志', log.includes('任务开始') && log.includes('任务结束'), log.slice(0, 100));

  // ---- 快捷操作
  d.querySelector('#nav button[data-tab="rec"]').click(); await sleep(300);
  d.querySelector('#recNav button[data-v="dash"]').click(); await sleep(700);
  check('概览运行状态使用键值排版', d.querySelectorAll('#dbCurrent .k').length >= 2, `${d.querySelectorAll('#dbCurrent .k').length}`);
  await w.eval('actLovePreview()'); await sleep(1500);                    // 预览任务真的在后台跑完
  const lp = await (await fetch(BASE + '/api/lb/runs')).json();
  const lrun = lp.runs.find(r => r.type === 'love_preview');
  check('红心预览任务成功完成（不再因假对象缺方法而失败）', lrun && lrun.status === 'done' && !lrun.summary, lrun ? `${lrun.status} ${lrun.summary}` : '没有该任务');
  w.eval('showLovePreview()'); await sleep(700);
  check('红心预览对话框显示真实跑出的结果', $('loveDlg').hasAttribute('open') && txt('loveMeta').includes('将提交 1 首') && txt('loveList').includes('红心歌'), txt('loveMeta'));
  check('预览里标明 MBID 来源', txt('loveList').includes('Navidrome 标签'), txt('loveList'));
  $('loveDlg').close();
  // ---- 任务记录：统计项都是中文且有说明；推荐联动任务在下载任务上面
  const stx = await w.eval("statsText({dedup_replaced:1,dup_suspects:1,via_item:1,via_lookup:1,via_navidrome:1,via_manual:1,unresolved:1,moved:1})");
  check('统计项没有英文键名', !/[a-z]_[a-z]|via|dedup|dup_/.test(stx), stx);
  const sth = await w.eval("statsHtml({dup_suspects:2})");
  check('统计项带悬停说明', sth.includes('title="') && sth.includes('选择保留哪一份'), sth);
  check('「任务记录」里推荐联动任务排在下载任务上面', !!(d.getElementById('runsSec').compareDocumentPosition(d.getElementById('jobsSec')) & 4), '');
  // ---- 解析不出 MBID 的红心：人工处理
  await w.eval('showUnresolved()'); await sleep(700);
  check('未解析对话框列出解析不出的歌和原因', $('unresDlg').hasAttribute('open') && txt('unresList').includes('歌手乙 - 野生歌') && txt('unresList').includes('没查到'), txt('unresList'));
  check('按钮上显示待处理数量', txt('unresN').includes('1'), txt('unresN'));
  await w.eval('unresSearch(0)'); await sleep(900);                       // 页面冒烟环境禁止外网：搜索失败时要有可读提示，且仍可粘贴
  check('搜索失败给出提示并保留粘贴入口', txt('unresBox0').includes('MusicBrainz') && !!d.getElementById('unresMbid0'), txt('unresBox0'));
  d.getElementById('unresMbid0').value = 'https://musicbrainz.org/recording/11111111-2222-3333-4444-555555555555';
  await w.eval('unresPaste2(0)'); await sleep(700);
  check('粘贴链接后自动提取 MBID 并移入「已手动处理」', txt('unresList').includes('没有待处理') && txt('unresDoneT').includes('1') && txt('unresDoneL').includes('11111111-2222'), txt('unresDoneL'));
  await w.eval('unresUndo(0)'); await sleep(700);
  check('撤销后手动记录消失', txt('unresDoneT').includes('0'), txt('unresDoneT'));
  $('unresDlg').close();
  // 清理：没有过期的 → toast
  await w.eval('actCleanup()'); await sleep(500);
  check('没有可清理时给出提示', txt('toast').includes('没有需要清理'), txt('toast'));
  // 转正：弹确认框，取消
  const countRuns = async () => (await (await fetch(BASE + '/api/lb/runs')).json()).runs.length;
  const before = await countRuns();
  const p = w.eval('actPromote()'); await sleep(200);
  check('转正先弹确认框', $('recConfirm').hasAttribute('open') && txt('rcBody').includes('不会覆盖'), txt('rcBody').slice(0, 60));
  $('recConfirm').close('no'); await p; await sleep(400);
  check('取消确认后没有产生新任务', (await countRuns()) === before, `${before}→${await countRuns()}`);
  // 409：任务正在运行 → 页面给出明确提示，不报脚本错误
  const realFetch = w.fetch;
  w.fetch = (u, o) => String(u).includes('/api/lb/action') ? Promise.resolve({ ok: false, status: 409, json: async () => ({ error: '任务正在运行：sync', busy: true, running: { type: 'sync' } }) }) : realFetch(u, o);
  await w.eval('actSync()'); await sleep(400);
  check('409 时提示「已有任务在运行」', txt('toast').includes('已有任务在运行'), txt('toast'));
  w.fetch = realFetch;
  // 400：门禁等错误原样显示
  w.fetch = (u, o) => String(u).includes('/api/lb/action') ? Promise.resolve({ ok: false, status: 400, json: async () => ({ error: '推荐联动还没有启用：请先在「设置」页的「推荐联动设置」里配置并打开总开关' }) }) : realFetch(u, o);
  await w.eval('actSync()'); await sleep(400);
  check('400 时显示后端给出的原因', txt('toast').includes('还没有启用'), txt('toast'));
  w.fetch = realFetch;
  const b2 = await countRuns();
  await w.eval('actSync()'); await sleep(1500);
  check('点「立即同步推荐」恰好产生 1 个任务', (await countRuns()) === b2 + 1, `${b2}→${await countRuns()}`);
  // 设置子页仍在
  d.querySelector('#nav button[data-tab="set"]').click(); await sleep(900);
  check('推荐设置已移入「设置」页（可折叠，默认折叠）', !!$('cLbLb') && $('lbSetBox').tagName === 'DETAILS' && !$('lbSetBox').open && $('lbUser').value === 'me', `lbUser=${$('lbUser').value}`);
  check('推荐联动设置只在「设置」页内，其它标签页没有', !!$('p-set').querySelector('#lbSetBox') && d.querySelectorAll('#lbSetBox').length === 1 && !$('p-rec').querySelector('#lbSetBox') && !!d.querySelector('link[rel=icon]'));
  check('推荐页不再有「任务」「设置」子页', ![...d.querySelectorAll('#recNav button')].some(b => ['runs', 'set'].includes(b.dataset.v)));
  check('推荐策略里可改 Navidrome 歌单名并带含义说明', $('pn-daily').value === 'LB Daily Jams {date}' && txt('stratRows').includes('探索新口味'), $('pn-daily').value);
  check('红心同步卡片存在', !!$('cLbLove'));
  check('设置页分三组：账号与下载 / 推荐联动 / 通知（另有术语说明）', ['sgAcct', 'lbSetBox', 'sgNotify', 'sgGloss'].every(i => $(i) && $(i).tagName === 'DETAILS'), '');
  check('账号与下载、通知默认展开，推荐联动默认折叠', $('sgAcct').open && $('sgNotify').open && !$('lbSetBox').open);
  check('各设置项落在正确的分组里', $('sgAcct').contains($('qSel')) && $('sgAcct').contains($('libType')) && $('sgAcct').contains($('dlSrc')) && $('sgNotify').contains($('bark')) && $('lbSetBox').contains($('lbUser')));
  check('术语说明包含主库/试听库/转正/MBID', ['主库', '试听库', '转正', 'MBID'].every(t => txt('sgGloss').includes(t)), '');


  console.log(logs.join('\n'));
  console.log('\n脚本错误数:', errors.length); errors.forEach(e => console.log('  ' + e));
  w.close(); process.exit(errors.length ? 1 : 0);
})().catch(e => { console.log(logs.join('\n')); console.log('测试脚本异常:', e); process.exit(2); });
