"""Единая таблица типов сигналов (ТЗ #5 §11): приведение и проверка реестра."""

from __future__ import annotations

import pytest

from app.config import load_signal_config
from app.services import signal_types as st


class TestCoerceApi:
    """Контракт ``api``: ручной запрос через REST."""

    def test_double_keeps_fraction(self):
        assert st.coerce("Double", 0.0856, contract="api") == 0.0856
        assert st.coerce("Double", -37.17, contract="api") == -37.17
        assert st.coerce("Double", 2, contract="api") == 2.0  # целое расширяется без потерь

    def test_int16_rejects_fraction_instead_of_truncating(self):
        with pytest.raises(st.SignalValueError, match="усечения"):
            st.coerce("Int16", 1.5, contract="api")

    def test_int16_range(self):
        assert st.coerce("Int16", "32767", contract="api") == 32767
        with pytest.raises(st.SignalValueError, match="Int16"):
            st.coerce("Int16", 40000, contract="api")

    def test_uint16_range(self):
        assert st.coerce("UInt16", 65535, contract="api") == 65535
        with pytest.raises(st.SignalValueError, match="UInt16"):
            st.coerce("UInt16", -1, contract="api")

    def test_boolean_from_string_words(self):
        assert st.coerce("Boolean", "1", contract="api") is True
        assert st.coerce("Boolean", "true", contract="api") is True
        assert st.coerce("Boolean", "0", contract="api") is False
        with pytest.raises(st.SignalValueError, match="Boolean"):
            st.coerce("Boolean", "возможно", contract="api")

    def test_api_rejects_number_as_boolean(self):
        # §11: произвольное число в Boolean не преобразуется.
        for bad in (1, 0, 0.5):
            with pytest.raises(st.SignalValueError):
                st.coerce("Boolean", bad, contract="api")

    def test_rejects_bool_for_numeric(self):
        # isinstance(True, int) — ловушка: логическое значение не число.
        with pytest.raises(st.SignalValueError):
            st.coerce("Int16", True, contract="api")
        with pytest.raises(st.SignalValueError):
            st.coerce("Double", False, contract="api")

    def test_rejects_null_and_empty_string(self):
        with pytest.raises(st.SignalValueError, match="null"):
            st.coerce("Int16", None, contract="api")
        with pytest.raises(st.SignalValueError, match="пустая"):
            st.coerce("Int16", "  ", contract="api")

    def test_rejects_nan_and_infinity(self):
        for bad in (float("nan"), float("inf"), float("-inf"), "nan", "inf"):
            with pytest.raises(st.SignalValueError, match="допустимым"):
                st.coerce("Double", bad, contract="api")


class TestCoerceFile:
    """Контракт ``file``: строка конфигурации, колонка тренда, ячейка архива."""

    def test_int_01_to_boolean_allowed(self):
        assert st.coerce("Boolean", 1, contract="file") is True
        assert st.coerce("Boolean", 0, contract="file") is False

    def test_float_01_to_boolean_allowed(self):
        assert st.coerce("Boolean", 1.0, contract="file") is True
        assert st.coerce("Boolean", 0.0, contract="file") is False

    def test_fraction_to_boolean_rejected_even_in_file(self):
        with pytest.raises(st.SignalValueError):
            st.coerce("Boolean", 0.5, contract="file")
        with pytest.raises(st.SignalValueError):
            st.coerce("Boolean", 2, contract="file")


class TestTypeTable:
    def test_unknown_type_is_config_error(self):
        with pytest.raises(st.SignalConfigError, match="неизвестный тип"):
            st.resolve_type("string")

    def test_legacy_spellings_resolve(self):
        assert st.resolve_type("bool").name == "Boolean"
        assert st.resolve_type("int").name == "Int32"
        assert st.resolve_type("float").name == "Double"
        assert st.resolve_type("double").name == "Double"
        assert st.resolve_type("long").name == "Int32"

    def test_defaults(self):
        assert st.default_for("Boolean") is False
        assert st.default_for("Int16") == 0
        assert st.default_for("UInt16") == 0
        assert st.default_for("Double") == 0.0


class TestValidateConfigs:
    def _cfg(self, **kw):
        base = {"id": "s1", "name": "S1", "type": "Boolean",
                "node_id": "Chanel1.Application.s1"}
        base.update(kw)
        return base

    def test_clean_registry_passes(self):
        st.validate_configs([self._cfg(), self._cfg(id="s2", node_id="Chanel1.Application.s2")],
                            source="test")

    def test_all_problems_reported_at_once(self):
        configs = [
            self._cfg(),
            self._cfg(id="s1"),  # дубль id
            self._cfg(id="s3", node_id="Chanel1.Application.s1"),  # дубль NodeId
            self._cfg(id="s4", type="string"),  # неизвестный тип
            self._cfg(id="s5", type="Int16", default=1.5),  # негодное начальное
        ]
        with pytest.raises(st.SignalConfigError) as exc:
            st.validate_configs(configs, source="test")
        text = str(exc.value)
        assert "дублирует" in text
        assert "неизвестный тип" in text
        assert "начальное значение" in text
        # Все проблемы одним списком, а не первая.
        assert text.count("\n  - ") >= 4

    def test_missing_node_falls_back_to_id(self):
        st.validate_configs([{"id": "only-id", "type": "Boolean"}], source="test")
        assert st.node_local_id({"id": "only-id"}) == "only-id"


class TestLoadSignalConfig:
    """Реестр приложения проверяется при загрузке (ТЗ #5 §5)."""

    def test_production_registry_loads(self):
        configs = load_signal_config()
        assert len(configs) == 101

    def test_unknown_type_in_bindings_fails_loudly(self, tmp_path):
        path = tmp_path / "bindings.json"
        path.write_text(
            '{"schema": "telemetry.opcua.input-bindings.v1", "bindings": ['
            '{"id": "x1", "name": "X1", "type": "string", "opcua_node": "ns=2;s=A.B"}]}',
            encoding="utf-8",
        )
        with pytest.raises(st.SignalConfigError, match="неизвестный тип"):
            load_signal_config(str(path))

    def test_duplicate_node_id_fails_loudly(self, tmp_path):
        path = tmp_path / "bindings.json"
        path.write_text(
            '{"schema": "telemetry.opcua.input-bindings.v1", "bindings": ['
            '{"id": "x1", "type": "Boolean", "opcua_node": "ns=2;s=A.B"},'
            '{"id": "x2", "type": "Boolean", "opcua_node": "ns=2;s=A.B"}]}',
            encoding="utf-8",
        )
        with pytest.raises(st.SignalConfigError, match="NodeId дублирует"):
            load_signal_config(str(path))


def test_main_coerce_delegates_and_translates_error():
    from fastapi import HTTPException

    from app.main import _coerce_value

    assert _coerce_value({"type": "Double"}, 0.5) == 0.5
    with pytest.raises(HTTPException) as exc:
        _coerce_value({"type": "Int16"}, 1.5)
    assert exc.value.status_code == 400
    assert "усечения" in exc.value.detail
