"""页面冒烟测试的后端：真实后端 + 假的外部系统（Navidrome/ListenBrainz/网易云），造一份有代表性的数据并起 HTTP 服务。
不属于 pytest，手动使用：见同目录 README.md。"""
import sys, os, json, threading, time
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
os.environ.pop('WEB_PASSWORD', None)
from pathlib import Path
import socket, tempfile

# ---- 隔离（重要）：数据目录是临时目录、不访问外网、通知置空。
# 曾经因为没隔离（读到真实 config.yaml 里的 Bark 地址），跑一次就给用户手机推送一堆 Bark 通知。
_real_getaddrinfo = socket.getaddrinfo
def _guard(host, *a, **k):
    if host not in (None, 'localhost', '127.0.0.1', '::1'):
        raise OSError(f'ui_server 禁止访问外部网络：{host}')
    return _real_getaddrinfo(host, *a, **k)
socket.getaddrinfo = _guard
import web_server, lb_settings, lb_repo, lb_states as ST
from test_lb_pipeline import FakeAdapter, FakeLB, FakeND, lbtrack, ndsong
from lb_adapter import AdapterResult

tmp = Path(tempfile.mkdtemp())
(tmp / 'dl').mkdir(); (tmp / 'songs').mkdir(); (tmp / 'discovery').mkdir()
app = web_server.App(tmp / 'data', tmp / 'dl')
class UiLB(FakeLB):
    """同步用的假 LB + 红心同步需要的方法。写入（love）一旦被调用就报错：页面冒烟测试绝不允许写 ListenBrainz。"""
    def loved_mbids(self): return []
    def lookup(self, artist, title): return None
    def love(self, mbid): raise AssertionError('页面冒烟测试不允许写入 ListenBrainz')


class UiND(FakeND):
    """带一首「已标红心」的歌，让红心预览有内容可看。"""
    def starred_songs(self, folder_id=None):
        return [{"id": "star2", "title": "野生歌", "artist": "歌手乙", "album": "", "mbid": "",
                 "starred": True, "duration": 200, "size": 1, "suffix": "flac", "album_artist": "歌手乙", "path_api": ""},
                {"id": "star1", "title": "红心歌", "artist": "歌手甲", "album": "", "mbid": "abcdef01-1111-1111-1111-111111111111",
                 "starred": True, "duration": 200, "size": 1, "suffix": "flac", "album_artist": "歌手甲", "path_api": ""}]


lb, nd = UiLB(), UiND()
adapter = FakeAdapter(nd, tmp / 'discovery')
svc = app.lb
svc.lb_factory, svc.nd_factory = (lambda s: lb), (lambda s: nd)
svc.adapter_factory = lambda s, d: adapter
svc.request_delay, svc.sleep = (lambda: 0), (lambda x: None)
svc.notify = lambda title, body: None                  # 页面冒烟测试不发 Bark 通知
app.account_view = lambda refresh=False: {"logged_in": True, "invalid": False, "uid": 1, "nickname": "测试", "avatar": "", "is_vip": True, "quality": "lossless", "mode": "VIP"}

s = lb_settings.defaults(); s['enabled'] = True
s['listenbrainz'].update(username='me', token='tok')
s['navidrome'].update(url='http://n', user='u', password='p', main_folder_id=1, discovery_folder_id=2)
s['dirs'].update(main=str(tmp / 'songs'), discovery=str(tmp / 'discovery'))
app.kv.set(lb_settings.SETTINGS_KEY, s)

# 造数据：一份 Weekly Jams，包含各种结局
nd.main = [ndsong('m1', 'A', 'Song 1')]
nd.indexed_disc = [ndsong('d2', 'A', 'Song 2')]
tracks = [lbtrack(i) for i in range(1, 8)]
lb.add('weekly_jams', 'plJ', tracks)
lb.add('daily', 'plD', [lbtrack(11, title='日推一'), lbtrack(12, title='日推二')])
adapter.script = {'Song 4': 'not_found', 'Song 5': 'download', 'Song 6': 'only_mp3'}
db = app.store.db
with app.tm.run('sync', 'manual') as h:
    from lb_pipeline import SyncDeps, run_sync
    run_sync(SyncDeps(db, s, lb, nd, adapter, h, request_delay=0, sleep=lambda x: None))
# 一首「试听库」歌与主库疑似重复（§17.16）：给第一个有本地文件的试听库条目造一个主库同曲，一个 exact 级
with db.transaction() as c:
    dup_item = c.execute("SELECT i.id FROM recommendation_items i JOIN tracks t ON t.id=i.track_id JOIN recommendation_batches b ON b.id=i.batch_id "
                        "WHERE b.type='weekly_jams' AND i.state='LOCAL_DISCOVERY' AND i.local_file_id IS NOT NULL ORDER BY i.id LIMIT 1").fetchone()['id']
    c.execute("INSERT INTO local_files(nd_song_id, library, artist, title, album, duration, size, suffix, indexed_at) "
              "VALUES('dupm','main','主库歌手','主库同名歌','主库专辑',231,31457280,'wav','t')")
lb_repo.add_dup_suspects(db, dup_item, [('dupm', 'exact', 25)])
# 一个 MULTIPLE_MATCH 条目 + 候选
bid = lb_repo.create_batch(db, 'weekly_exploration', {'mbid': 'plE', 'title': 't', 'date': '2026-11-16T00:00:00+00:00'},
    {'tracks': [{'recording_mbid': 'mbE1', 'artist': '多歌手', 'title': '难选的歌', 'album': '专辑', 'duration_ms': 200000, 'artists': []}]})
item = lb_repo.items_of_batch(db, bid)[0]['id']
with db.transaction() as c:
    c.execute("INSERT INTO local_files(nd_song_id, library, artist, title, album, duration, indexed_at) VALUES('c1','main','多歌手','难选的歌','专辑甲',200,'t'),('c2','discovery','多歌手','难选的歌','专辑乙',401,'t')")
class F:
    def __init__(self, i, l): self.nd_song_id, self.library = i, l
lb_repo.save_candidates(db, item, [F('c1', 'main'), F('c2', 'discovery')])
lb_repo.set_state(db, item, ST.MULTIPLE_MATCH, reason_kind='multiple', reason='x')
app.tm.recover()

server = web_server.serve(os.environ.get('UI_HOST', '127.0.0.1'), int(os.environ.get('UI_PORT', '0')), app)
threading.Thread(target=server.serve_forever, daemon=True).start()
print(server.server_address[1], flush=True)
time.sleep(int(os.environ.get('UI_SECONDS', '600')))      # 手动演示时可设很长，如 UI_SECONDS=86400
