"""真实 ffmpeg + SIGINT 验证：不能在等待工作线程时遗留子进程。"""

import os
import shutil
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.mark.integration
@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGINT；Windows 控制台中断需实机验证")
def test_sigint_reaps_real_ffmpeg_and_stops_workers(tmp_path: Path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("需要真实 ffmpeg")
    script = tmp_path / "cancel.py"
    pid_path = tmp_path / "child.pid"
    script.write_text(
        textwrap.dedent("""
        import subprocess
        import sys
        from pathlib import Path
        from ivdc.runner import ProcessRunner, worker_pool
        original = subprocess.Popen
        def spawn(*args, **kwargs):
            child = original(*args, **kwargs)
            Path(sys.argv[2]).write_text(str(child.pid))
            return child
        subprocess.Popen = spawn
        runner = ProcessRunner()
        try:
            with runner, worker_pool(runner, 1) as pool:
                job = pool.submit(runner.stream, [sys.argv[1], '-hide_banner', '-nostdin',
                    '-v', 'error', '-re', '-f', 'lavfi', '-i', 'color=size=32x32:rate=10',
                    '-t', '30', '-progress', 'pipe:1', '-f', 'null', '-'],
                    on_output=lambda line: print('ready', flush=True))
                job.result()
        except KeyboardInterrupt:
            print(f'reaped={runner.active_count}', flush=True)
            sys.exit(1)
    """),
        encoding="utf-8",
    )
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    process = subprocess.Popen(
        [sys.executable, "-u", str(script), ffmpeg, str(pid_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.stdout is not None
        # select gives a bounded readiness wait on POSIX.
        import select

        assert select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline().strip() == "ready"
        child_pid = int(pid_path.read_text())
        process.send_signal(signal.SIGINT)
        output, error = process.communicate(timeout=10)
        assert process.returncode == 1, error
        assert "reaped=0" in output
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
