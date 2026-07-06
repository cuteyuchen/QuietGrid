from __future__ import annotations

from pathlib import Path


def test_trader_systemd_service_runs_binance_loop_with_env_file() -> None:
    service = Path("deploy/systemd/quietgrid-trader.service").read_text(encoding="utf-8")

    assert "ExecStart=/home/ubuntu/quietgrid/.venv/bin/python trader.py --binance-loop" in service
    assert "EnvironmentFile=/home/ubuntu/quietgrid/.env" in service
    assert "Restart=always" in service


def test_web_systemd_service_uses_python_entrypoint() -> None:
    service = Path("deploy/systemd/quietgrid-web.service").read_text(encoding="utf-8")

    assert "ExecStart=/home/ubuntu/quietgrid/.venv/bin/python web.py" in service
    assert "streamlit run web.py" not in service
