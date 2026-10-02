"""适配层协议与命名回归，不访问网络。"""

from types import SimpleNamespace

from ivdc.config import Config
from ivdc.download import DownloadProgress
from ivdc.parse import TaskLine
from ivdc.ytdlp import (
    DownloadedFile,
    ProgressHook,
    YtdlpFetcher,
    collect_downloads,
    finalize_downloads,
    impersonate_options,
)


def test_explicit_title_controls_output_name(tmp_path):
    path = tmp_path / "extractor.mp4"
    path.write_bytes(b"video")
    result = finalize_downloads([DownloadedFile("remote title", path)], "My / title")
    assert result.title == "My / title"
    assert (tmp_path / "My _ title.mp4").read_bytes() == b"video"
    assert not path.exists()


def test_download_name_collision_preserves_both_files(tmp_path, caplog):
    raw, existing = tmp_path / "raw.mp4", tmp_path / "title.mp4"
    raw.write_bytes(b"new")
    existing.write_bytes(b"old")
    result = finalize_downloads([DownloadedFile("title", raw)])
    assert result.size_bytes == 3
    assert raw.read_bytes() == b"new"
    assert existing.read_bytes() == b"old"
    assert "目标名已存在" in caplog.text


def test_adapter_prefers_final_merged_path_and_flattens_playlist(tmp_path):
    merged, audio = tmp_path / "merged.mp4", tmp_path / "audio.m4a"
    merged.write_bytes(b"av")
    audio.write_bytes(b"a")
    payload = {
        "entries": [
            None,
            {
                "entries": [
                    {
                        "title": "title",
                        "filepath": str(merged),
                        "requested_downloads": [{"filepath": str(audio)}],
                    }
                ]
            },
        ]
    }
    downloader = SimpleNamespace(prepare_filename=lambda info: "unexpected")
    assert collect_downloads(downloader, payload) == [DownloadedFile("title", merged)]


def test_progress_external_values_are_parsed_at_boundary():
    seen = []
    hook = ProgressHook(seen.append)
    hook(
        {
            "status": "downloading",
            "downloaded_bytes": 50,
            "total_bytes_estimate": 100,
            "speed": float("nan"),
            "eta": -1,
        }
    )
    hook({"status": "finished"})
    assert seen == [DownloadProgress(50, 100), DownloadProgress(finished=True)]


def test_impersonation_domain_is_hostname_not_url_substring(monkeypatch):
    def must_not_import(name):
        raise AssertionError("unrelated host must not enable impersonation")

    monkeypatch.setattr("ivdc.ytdlp.importlib.import_module", must_not_import)
    config = Config(("example.com",))
    assert impersonate_options("https://evil.example/?next=example.com", config) == {}
    assert impersonate_options("https://example.com.evil.test", config) == {}


def test_optional_impersonation_dependency_can_degrade(monkeypatch, caplog):
    def unavailable(name):
        raise ImportError("missing optional dependency")

    monkeypatch.setattr("ivdc.ytdlp.importlib.import_module", unavailable)
    assert impersonate_options("https://sub.example.com/v", Config(("example.com",))) == {}
    assert "本次不做伪装" in caplog.text


def test_fetcher_adapts_ytdlp_download_without_network(tmp_path, monkeypatch):
    output = tmp_path / "remote.mp4"
    output.write_bytes(b"downloaded")
    calls = []

    class FakeDownloader:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download):
            calls.append((url, download))
            self.options["progress_hooks"][0]({"status": "finished"})
            return {"title": "remote", "filepath": str(output)}

        def prepare_filename(self, info):
            return str(output)

    fetch = YtdlpFetcher(Config())
    monkeypatch.setattr(fetch.module, "YoutubeDL", FakeDownloader)
    updates = []
    result = fetch(TaskLine("CON", "https://example.com/v"), updates.append)
    assert result.title == "CON"
    assert result.size_bytes == 10
    assert (tmp_path / "_CON.mp4").read_bytes() == b"downloaded"
    assert calls == [("https://example.com/v", True)]
    assert updates == [DownloadProgress(finished=True)]


def test_unsupported_impersonate_target_degrades_before_download(tmp_path, monkeypatch, caplog):
    import yt_dlp

    path = tmp_path / "video.mp4"
    path.write_bytes(b"x")

    class FakeDownloader:
        def __init__(self, options):
            self.params = dict(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def _impersonate_target_available(self, target):
            return False

        def extract_info(self, url, download):
            assert "impersonate" not in self.params
            return {"filepath": str(path), "title": "video"}

        def prepare_filename(self, info):
            return str(path)

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeDownloader)
    monkeypatch.setattr(
        "ivdc.ytdlp.impersonate_options", lambda *args: {"impersonate": "unavailable"}
    )
    result = YtdlpFetcher(Config())(TaskLine(None, "https://example.com/v"), lambda _: None)
    assert result.size_bytes == 1
    assert "本次不做伪装" in caplog.text
