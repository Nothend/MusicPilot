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
  check('一级导航只有：推荐 / 记录 / 设置（下载不占一级标签）', navTabs.join(',') === 'rec,jobs,set' && !d.querySelector('#nav [data-tab="dl"]'), navTabs.join(','));
  check('推荐页顶部工具栏：获取推荐 / 下载 / 设置 / 手动下载，与「概览｜推荐歌单」同一行', !!$('p-rec').querySelector('.rechead #recNav') && ['recSyncBtn', 'recDlBtn', 'recSetBtn', 'btnManual'].every(i => !!$('p-rec').querySelector('.rechead .rectools #' + i)));
  check('默认打开「推荐」', $('p-rec').classList.contains('on') && !$('p-dl').classList.contains('on'));
  await sleep(600);
  check('「需要你处理」卡片列出待选择候选', !$('todoCard').classList.contains('hide') && txt('todoList').includes('待选择'), txt('todoList'));
  check('「推荐」标签上有待处理角标', /\d/.test(txt('navRec')), txt('navRec'));
  await w.eval("todoGo('multiple')"); await sleep(900);
  check('点「去选择」直达列表并筛到「待选择」', !$('rv-list').classList.contains('hide') && txt('lstStates').includes('待选择') && $('lstItems').children.length >= 1 && !txt('lstStates').includes('下载失败'), txt('lstStates'));
  await w.eval("recShow('dash')");
  $('btnManual').click(); await sleep(300);
  check('点「手动下载…」进二级页，导航仍高亮「推荐」', $('p-dl').classList.contains('on') && d.querySelector('#nav button.on').dataset.tab === 'rec');
  check('「手动下载」默认是手动下载', !$('dv-manual').classList.contains('hide') && $('dv-auto').classList.contains('hide'));
  d.querySelector('#dlNav button[data-v="auto"]').click(); await sleep(300);
  check('切到「定时任务」子页', $('dv-manual').classList.contains('hide') && !$('dv-auto').classList.contains('hide') && !!$('schedList'));
  w.showTab('auto'); await sleep(200);
  check('旧链接 #auto 仍能打开（定时任务）', $('p-dl').classList.contains('on') && !$('dv-auto').classList.contains('hide'));
  d.querySelector('#dlNav button[data-v="manual"]').click();
  $('dlBack').click(); await sleep(300);
  check('「← 返回推荐」回到推荐页', $('p-rec').classList.contains('on') && !$('p-dl').classList.contains('on'));
  w.showTab('dl'); check('旧链接 #dl 仍能打开手动下载', $('p-dl').classList.contains('on')); w.showTab('rec');

  // ---- 概览
  w.showTab('rec'); await sleep(900);
  check('概览子页可见', !$('rv-dash').classList.contains('hide') && $('rv-list').classList.contains('hide'));
  check('没有「总开关」状态标记，配置齐全时不显示「推荐还不能运行」', !$('dbEnabled') && $('recOff').classList.contains('hide'));
  { const rf = w.fetch;
    w.fetch = async (u, o) => { const r = await rf(u, o); if (!String(u).includes('/api/lb/dashboard')) return r; const j = await r.json(); j.setup_issue = '请先在设置里填写 ListenBrainz 用户名和 Token'; return { ok: true, status: 200, json: async () => j }; };
    await w.eval('dashLoad()'); w.fetch = rf;
    check('缺配置时，推荐页顶部出现「推荐还不能运行」并写明缺什么、可去设置', !$('recOff').classList.contains('hide') && txt('recOffMsg').includes('ListenBrainz 用户名和 Token') && !!$('recOff').querySelector('button'), txt('recOffMsg'));
    await w.eval('dashLoad()'); }
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
  check('原因中文化', t.includes('音乐平台没有这首') && t.includes('只有 MP3'), '');
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
  check('点「重试」后该条回到待处理（页面上称「待下载」）', txt('lstItems').includes('待下载'));
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
  await w.eval('actSyncHere()'); await sleep(400);
  check('409 时提示「已有任务在运行」', txt('toast').includes('已有任务在运行'), txt('toast'));
  w.fetch = realFetch;
  // 400：门禁等错误原样显示
  w.fetch = (u, o) => String(u).includes('/api/lb/action') ? Promise.resolve({ ok: false, status: 400, json: async () => ({ error: '请先在设置里填写 Navidrome 地址、用户名和密码' }) }) : realFetch(u, o);
  await w.eval('actSyncHere()'); await sleep(400);
  check('400 时显示后端给出的原因', txt('toast').includes('Navidrome 地址'), txt('toast'));
  w.fetch = realFetch;
  const b2 = await countRuns();
  await w.eval('actSyncHere()'); await sleep(1500);
  check('点「获取推荐」恰好产生 1 个任务', (await countRuns()) === b2 + 1, `${b2}→${await countRuns()}`);
  // 设置子页仍在
  d.querySelector('#nav button[data-tab="set"]').click(); await sleep(900);
  check('推荐来源设置在「设置」页的第一组且默认展开', $('sgSrc').tagName === 'DETAILS' && $('sgSrc').open && $('sgSrc').contains($('cLbLb')) && $('lbUser').value === 'me', `lbUser=${$('lbUser').value}`);
  check('推荐相关设置只在「设置」页内，其它标签页没有', !!$('p-set').querySelector('#sgSrc') && !$('p-rec').querySelector('#sgSrc') && !$('p-rec').querySelector('#sgLib') && !!d.querySelector('link[rel=icon]'));
  check('推荐页不再有「任务」「设置」子页', ![...d.querySelectorAll('#recNav button')].some(b => ['runs', 'set'].includes(b.dataset.v)));
  check('推荐策略里可改 Navidrome 歌单名并带含义说明', $('pn-daily').value === 'LB Daily Jams {date}' && txt('stratRows').includes('探索新口味'), $('pn-daily').value);
  check('红心同步卡片存在', !!$('cLbLove'));
  const sgIds = ['sgSrc', 'sgAcct', 'sgLib', 'sgNotify', 'sgGloss'];
  check('设置页分组顺序：推荐来源 / 下载渠道 / 音乐库与整理 / 通知 / 术语说明', sgIds.every(i => $(i) && $(i).tagName === 'DETAILS') && [...$('cSet').children].map(e => e.id).join(',') === sgIds.join(','), [...$('cSet').children].map(e => e.id).join(','));
  check('推荐来源、下载渠道、通知默认展开，音乐库与整理默认折叠', $('sgSrc').open && $('sgAcct').open && $('sgNotify').open && !$('sgLib').open);
  check('各设置项落在正确的分组里',
    ['lbSource', 'lbUser', 'lbToken', 'sDaily', 'sWeekly', 'sPromote', 'pn-daily'].every(i => $('sgSrc').contains($(i))) &&
    ['qSel', 'dlSrc', 'delay'].every(i => $('sgAcct').contains($(i))) && !$('sgAcct').contains($('libType')) && $('sgLib').contains($('libType')) && !$('cLbOn') && !$('lbEnabled') &&
    ['ndUrl', 'ndMain', 'dirMain', 'dRet', 'dPromote', 'loveOn'].every(i => $('sgLib').contains($(i))) && $('sgNotify').contains($('bark')));
  // ---- 推荐来源：选择后设置页与推荐页只显示该来源的内容
  const vis = el => !el.classList.contains('hide');
  check('默认来源是 ListenBrainz：显示其账号与每日/每周同步，不显示每日推荐同步', $('lbSource').value === 'listenbrainz' && vis($('cLbLb')) && vis($('sDaily').closest('.f')) && !vis($('sNeDaily').closest('.f')) && !!$('st-weekly_jams-on'));
  check('推荐来源选项含 ListenBrainz 与音乐平台每日推荐', [...$('lbSource').options].map(o => o.value).join(',') === 'listenbrainz,netease');
  $('lbSource').value = 'netease'; $('lbSource').dispatchEvent(new w.Event('change')); await sleep(700);      // 切换即自动保存
  check('切到音乐平台每日推荐后：隐藏 ListenBrainz 账号与每周同步，显示每日推荐同步', $('lbSource').value === 'netease' && !vis($('cLbLb')) && !vis($('sDaily').closest('.f')) && vis($('sNeDaily').closest('.f')));
  check('推荐策略只剩「每日推荐」一项且文案为每天自动获取推荐', !!$('st-netease_daily-on') && !$('st-weekly_jams-on') && txt('stratRows').includes('每天自动获取推荐'), txt('stratRows').slice(0, 80));
  d.querySelector('#nav button[data-tab="rec"]').click(); await sleep(300);
  await w.eval("recShow('list')"); await sleep(900);
  check('推荐歌单页只有一个列表时不显示切换条，按钮只有「每日推荐」，说明里也是', $('lstType').classList.contains('hide') && [...d.querySelectorAll('#lstType button')].map(b => b.dataset.v).join(',') === 'netease_daily' && txt('lstDesc').includes('每日推荐') && !txt('lstDesc').includes('ListenBrainz 的'), txt('lstDesc'));
  await w.eval("recShow('dash')"); await sleep(900);
  check('概览里不再出现 ListenBrainz 的每日熟悉/每周批次，空状态文案按来源', !txt('dbBatches').includes('每周') && txt('dbBatches').includes('每日推荐'), txt('dbBatches'));
  d.querySelector('#nav button[data-tab="set"]').click(); await sleep(600);
  $('lbSource').value = 'listenbrainz'; $('lbSource').dispatchEvent(new w.Event('change')); await sleep(700);
  check('切回 ListenBrainz 后恢复显示', vis($('cLbLb')) && !!$('st-weekly_jams-on') && !$('st-netease_daily-on'));
  await w.eval("recShow('list')"); d.querySelector('#nav button[data-tab="rec"]').click(); await sleep(900);
  check('切回后推荐歌单页恢复三个列表按钮', [...d.querySelectorAll('#lstType button')].map(b => b.dataset.v).join(',') === 'daily,weekly_exploration,weekly_jams' && !$('lstType').classList.contains('hide'));
  d.querySelector('#nav button[data-tab="set"]').click(); await sleep(400);
  // ---- 联动开关：默认（全新安装）关闭；打开才出现 Navidrome 相关的一切；切换即保存，打开前要确认
  check('联动已开（本测试数据按联动造的）：Navidrome / 目录 / 试听库 / 红心同步卡片可见，下载目录与音乐库去重卡片隐藏', $('lbLink').checked && vis($('cLbMode')) && vis($('cLbNd')) && vis($('cLbDisc')) && vis($('cLbLove')) && !vis($('cLbOut')) && !vis($('cLbDedupe')));
  check('下载渠道选择在「下载渠道」组里，目前只有音乐平台', $('sgAcct').contains($('dlChannel')) && [...$('dlChannel').options].map(o => o.value).join(',') === 'platform' && txt('dlChannelDesc').length > 0);
  $('lbLink').checked = false; $('lbLink').dispatchEvent(new w.Event('change')); await sleep(700);        // 关闭联动：不需要确认，立即保存
  check('关闭联动：隐藏 Navidrome / 目录 / 试听库 / 红心同步 / 联动专属定时与歌单名，出现下载目录与音乐库去重', ['cLbNd', 'cLbDir', 'cLbDisc', 'cLbLove'].every(i => !vis($(i))) && !vis($('sPromote').closest('.f')) && !vis($('sClean').closest('.f')) && !vis($('sLove').closest('.f')) && !vis($('pn-daily').closest('.f')) && vis($('cLbMode')) && vis($('cLbOut')) && vis($('cLbDedupe')));
  $('outDir').value = '/tmp/ui_out_dir'; await w.eval("lbSave('output')"); await sleep(500);
  await w.eval("lbTest('output','output','lbOutRes')"); await sleep(600);
  check('下载目录已保存，检查目录给出结果', $('outDir').value === '/tmp/ui_out_dir' && txt('lbOutRes').includes('下载目录可用'), txt('lbOutRes'));
  d.querySelector('#nav button[data-tab="rec"]').click(); await sleep(300);
  await w.eval("recShow('dash')"); await sleep(900);
  const qaBtns = [...d.querySelectorAll('#rv-dash .qa button')].filter(b => !b.closest('.hide')).map(b => b.textContent);
  check('概览（未联动）：快捷操作区整组隐藏（没有转正/清理/索引/红心）', qaBtns.length === 0, qaBtns.join(','));
  check('概览（未联动）：统计只有下载相关的 7 项', $('dbTiles').children.length === 7 && txt('dbTiles').includes('已下载') && txt('dbTiles').includes('待下载') && !txt('dbTiles').includes('待清理'), txt('dbTiles'));
  // ---- 推荐页入口：设置按钮、列表页的「立即获取推荐」与设置直达
  const tipOf = id => $(id).getAttribute('data-tip') || '';
  check('工具栏按钮都有气泡提示；图标按钮另带无障碍名称', ['recSyncBtn', 'recDlBtn', 'recSetBtn', 'btnManual'].every(i => tipOf(i).length >= 2) && $('recSetBtn').getAttribute('aria-label') === '设置' && $('btnManual').getAttribute('aria-label') === '手动下载');
  check('「获取推荐」「下载」带文字，设置与手动下载是纯图标', $('recSyncBtn').textContent.trim() === '获取推荐' && /^下载/.test($('recDlBtn').textContent.trim()) && $('recSetBtn').textContent.trim() === '' && $('btnManual').textContent.trim() === '');
  $('recSetBtn').click(); await sleep(100);
  check('点 ⚙ 弹出菜单：推荐来源设置 / 下载设置 / 联动设置', !$('setMenu').classList.contains('hide') && [...$('setMenu').querySelectorAll('button')].map(b => b.textContent).join(',') === '推荐来源设置,下载设置,联动设置');
  d.body.click(); await sleep(50);
  check('点菜单外面菜单收起', $('setMenu').classList.contains('hide'));
  await w.eval("recShow('list')"); await sleep(900);
  check('推荐歌单页本身不再有那一排按钮（统一在顶部工具栏）', ![...d.querySelectorAll('#rv-list .tools button')].some(b => ['获取推荐', '推荐来源设置', '下载设置', '联动设置'].includes(b.textContent.trim())) && vis($('recNav')) );
  const sent = []; const rf0 = w.fetch;
  w.fetch = (u, o) => { if (String(u).includes('/api/lb/action') && o && o.body) sent.push(JSON.parse(o.body)); return rf0(u, o); };
  $('recSyncBtn').click(); await sleep(400);
  check('「获取推荐」请求明确不下载（download=false），与设置里的自动下载无关', sent.length === 1 && sent[0].kind === 'sync' && sent[0].download === false, JSON.stringify(sent));
  check('点「获取推荐」留在本页，提示只获取不下载', $('p-rec').classList.contains('on') && !$('rv-list').classList.contains('hide') && txt('toast').includes('只获取，不下载'), txt('toast'));
  for (let i = 0; i < 40 && $('recSyncBtn').disabled; i++) await sleep(500);
  check('任务结束后按钮恢复可点（列表自动刷新）', !$('recSyncBtn').disabled);
  $('recSyncBtn').click();
  let sawDisabled = false;                                                                   // 任务很短：连续采样，只要运行期间禁用过就算
  for (let i = 0; i < 60 && !sawDisabled; i++) { await sleep(25); sawDisabled = $('recSyncBtn').disabled && $('recDlBtn').disabled; }
  check('任务运行中顶部「获取推荐」「下载」被禁用', sawDisabled);
  d.querySelector('#nav button[data-tab="set"]').click(); await sleep(4000);               // 任务期间离开推荐页
  d.querySelector('#nav button[data-tab="rec"]').click(); await sleep(1500);
  check('开始获取后切到别的标签再回来，按钮不会一直禁用', !$('recSyncBtn').disabled && !$('recDlBtn').disabled);
  await w.eval("recShow('dash')"); await sleep(600);
  $('recSyncBtn').click(); await sleep(40);                                                  // 在概览页里同样的场景
  d.querySelector('#nav button[data-tab="set"]').click(); await sleep(4000);
  d.querySelector('#nav button[data-tab="rec"]').click(); await sleep(1500);
  check('概览页：开始获取后切走再回来，按钮同样不会一直禁用', !$('recSyncBtn').disabled && !$('recDlBtn').disabled);
  await w.eval("recShow('list')"); await sleep(600);
  $('recSetBtn').click(); await sleep(100); $('setMenu').querySelectorAll('button')[2].click(); await sleep(600);
  check('菜单里点「联动设置」跳到设置页并展开「音乐库与整理」', $('p-set').classList.contains('on') && $('sgLib').open);
  // ---- 推荐为主、下载为辅：获取只显示歌单；「下载」才下载；设置里的「自动下载」只管定时同步，默认关闭
  const dash = await (await realFetch('/api/lb/dashboard')).json();
  const withPending = Object.entries(dash.latest_batches).find(([t, b]) => (b.counts.PENDING || 0) > 0);
  check('测试数据里有待下载的批次', !!withPending, JSON.stringify(Object.entries(dash.latest_batches).map(([t, b]) => [t, b.counts])));
  d.querySelector('#nav button[data-tab="rec"]').click(); await sleep(300);
  if (withPending) await w.eval(`openList('${withPending[0]}', ${withPending[1].batch_id})`);
  await sleep(900);
  const pend = [...d.querySelectorAll('#lstItems .rit')].filter(r => r.textContent.includes('待下载'));
  check('获取推荐后，本地没有的歌显示「待下载」并带单首「下载」按钮', pend.length >= 1 && pend.every(r => [...r.querySelectorAll('button')].some(b => b.textContent === '下载')), `${pend.length} 首待下载`);
  check('「下载」按钮显示待下载数量', /^下载（\d+）$/.test($('recDlBtn').textContent), $('recDlBtn').textContent);
  const runsBefore = await countRuns();
  sent.length = 0;
  pend[0].querySelector('button.sm').click(); await sleep(500);
  check('单首「下载」请求明确要下载并带条目编号', sent.length === 1 && sent[0].download === true && Number.isInteger(sent[0].item_id), JSON.stringify(sent));
  check('点单首「下载」：提示开始下载，并产生 1 个同步任务', txt('toast').includes('开始下载这一首') && (await countRuns()) === runsBefore + 1, txt('toast'));
  for (let i = 0; i < 40 && $('recDlBtn').disabled; i++) await sleep(500);
  check('下载任务结束后按钮恢复', !$('recDlBtn').disabled && !$('recSyncBtn').disabled);
  d.querySelector('#nav button[data-tab="set"]').click(); await sleep(400);
  const adl0 = $('autoDl').checked;
  $('autoDl').checked = !adl0; $('autoDl').dispatchEvent(new w.Event('change')); await sleep(600);
  check('「自动下载」在「下载渠道」组里，切换即保存', $('sgAcct').contains($('autoDl')) && (await (await realFetch('/api/lb/settings')).json()).auto_download === !adl0);
  $('autoDl').checked = adl0; $('autoDl').dispatchEvent(new w.Event('change')); await sleep(600);
  d.querySelector('#nav button[data-tab="set"]').click(); await sleep(300);
  // 打开联动：先确认；取消则保持关闭
  $('lbLink').checked = true; $('lbLink').dispatchEvent(new w.Event('change')); await sleep(200);
  check('打开联动前弹出确认，说明会转正与清理', $('recConfirm').hasAttribute('open') && txt('rcBody').includes('回收站'), txt('rcBody').slice(0, 60));
  $('recConfirm').close('no'); await sleep(400);
  check('取消后开关恢复为关闭，且没有保存', !$('lbLink').checked && !vis($('cLbNd')) && (await (await realFetch('/api/lb/settings')).json()).mode === 'folder');
  $('lbLink').checked = true; $('lbLink').dispatchEvent(new w.Event('change')); await sleep(200);
  $('rcOk').click(); await sleep(800);
  check('确认后联动打开并保存，Navidrome 相关卡片恢复', $('lbLink').checked && ['cLbNd', 'cLbDir', 'cLbDisc', 'cLbLove'].every(i => vis($(i))) && !vis($('cLbOut')) && !vis($('cLbDedupe')) && vis($('sPromote').closest('.f')));
  check('术语说明包含主库/试听库/转正/MBID', ['主库', '试听库', '转正', 'MBID'].every(t => txt('sgGloss').includes(t)), '');


  console.log(logs.join('\n'));
  console.log('\n脚本错误数:', errors.length); errors.forEach(e => console.log('  ' + e));
  w.close(); process.exit(errors.length ? 1 : 0);
})().catch(e => { console.log(logs.join('\n')); console.log('测试脚本异常:', e); process.exit(2); });
