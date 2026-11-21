"""Disposable cluster guard and fixture scope must not mutate a customer cluster."""

import importlib.util
from pathlib import Path
from unittest.mock import patch
import pytest

spec = importlib.util.spec_from_file_location(
    "gateway_smoke",
    Path(__file__).resolve().parents[2] / "scripts/gateway_acceptance.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize(
    "context,server",
    [
        ("production", "https://127.0.0.1:6443"),
        (module.CONTEXT, "https://cluster.example:6443"),
        (module.CONTEXT, "http://127.0.0.1:6443"),
        (module.CONTEXT, "https://operator@localhost:6443"),
    ],
)
def test_gateway_acceptance_refuses_non_disposable_targets(context, server):
    with pytest.raises(RuntimeError):
        module.validate_target(context, server)


def test_gateway_acceptance_does_nothing_without_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("SENTINEL_GATEWAY_ACCEPTANCE", raising=False)
    with patch.object(module, "kube") as kube:
        with pytest.raises(SystemExit):
            module.main()
        kube.assert_not_called()


def test_named_loopback_cluster_is_allowed():
    module.validate_target(module.CONTEXT, "https://127.0.0.1:6443")
