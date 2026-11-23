"""下载渠道注册表：把一首推荐歌曲（艺人 + 歌名，可带来源已知的平台歌曲 id）变成目录里的一个音频文件。

渠道约定（以后接入别的下载工具，只需实现这个接口并在 lb_settings.CHANNELS 里登记）：

    channel.download_song(artist, title, output_dir, *, artists=None, album="", duration_ms=None,
                          platform_id=None) -> lb_adapter.AdapterResult

- 成功：音频文件必须已经**原子地**落在 output_dir 下（先写 output_dir/.staging 再改名），标签完整；
  AdapterResult.path 是最终路径。同名合格文件已存在就复用（reused=True，幂等）。
- 失败：不抛异常，返回 AdapterResult(success=False, error_kind=...)：
    not_found / only_mp3 / no_copyright（该渠道没有可用版本，进入「未找到」冷却）、
    rate_limited（渠道被限流：整轮暂停，这首退回待处理，绝不记成未找到）、
    其余（download / api / invalid …）= 可重试的失败。
- platform_id 只有来源自带平台歌曲 id 时才会传；不认识它的渠道忽略即可（用 **kw 接收）。
- 渠道对象由工厂创建（一轮同步创建一次），需要的账号等信息从 store/settings 取；账号不可用抛 lb_service.NotReady。

目前只有 platform（音乐平台）。外部下载工具不在本镜像容器内时，可实现成「把请求写到某个目录、从某个目录收结果」的渠道，
仍然遵守上面的约定即可，流水线、页面都不用改。
"""

from typing import Any, Callable, Dict


def _platform(store, settings: Dict[str, Any], request_delay: float):
    from lb_service import default_adapter_factory     # 延迟导入：避免与 lb_service 循环依赖
    return default_adapter_factory(store, settings, request_delay)


FACTORIES: Dict[str, Callable[[Any, Dict[str, Any], float], Any]] = {"platform": _platform}


def make(channel: str, store, settings: Dict[str, Any], request_delay: float):
    try:
        factory = FACTORIES[channel]
    except KeyError:
        raise ValueError(f"未知的下载渠道：{channel}")
    return factory(store, settings, request_delay)
