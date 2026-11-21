import shutil
from pathlib import Path

import pytest

from services import rules_service as rs
from settings import ROOT

RULES = ROOT / "config" / "rules_config.yaml"


def test_yaml_oficial_del_proyecto_es_valido():
    loaded = rs.load_rules(RULES)
    r = loaded.rules
    assert r.predial.min_year == 2020 and r.predial.requires_committee_confirmation is True
    assert r.documents.required == ["escrituras", "predial", "alineamiento_numero_oficial", "uso_suelo"]
    assert r.tolerances.surface_difference_percent is None and r.tolerances.surface_difference_m2 is None
    assert loaded.reference()["sha256"] and loaded.version


def test_reglas_de_uso_de_suelo_separadas_para_tienda_y_cedis():
    lu = rs.load_rules(RULES).rules.land_use
    assert lu.store.provisional_hints.positive != lu.cedis.provisional_hints.positive
    assert lu.store.allowed_categories == [] and lu.cedis.allowed_categories == []  # no se inventan categorias juridicas
    assert lu.store.level == "provisional_mvp"


def test_yaml_con_sintaxis_invalida():
    with pytest.raises(rs.RulesError, match="YAML"):
        rs.validate_yaml_text("rules: [esto no cierra")


def test_yaml_con_clave_mal_escrita_o_faltante():
    text = RULES.read_text(encoding="utf-8").replace("min_year: 2020", "min_yeer: 2020")
    with pytest.raises(rs.RulesError, match="min_yeer|min_year"):
        rs.validate_yaml_text(text)


def test_actualizar_reglas_registra_historial_y_respalda(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    monkeypatch.setenv("INMO_STORAGE_DIR", str(storage))
    target = tmp_path / "rules.yaml"
    shutil.copy(RULES, target)
    new_text = RULES.read_text(encoding="utf-8").replace("min_year: 2020", "min_year: 2021")
    loaded = rs.update_rules(new_text, actor="abogado", path=target)
    assert loaded.rules.predial.min_year == 2021
    hist = rs.rules_history()
    assert len(hist) == 1 and hist[0]["actor"] == "abogado" and hist[0]["sha256"] == loaded.sha256
    assert list((storage / "rules_backups").glob("*.yaml"))


def test_update_con_yaml_invalido_no_modifica_nada(tmp_path, monkeypatch):
    monkeypatch.setenv("INMO_STORAGE_DIR", str(tmp_path / "storage"))
    target = tmp_path / "rules.yaml"
    shutil.copy(RULES, target)
    before = target.read_text(encoding="utf-8")
    with pytest.raises(rs.RulesError):
        rs.update_rules("meta: {version: x}", path=target)
    assert target.read_text(encoding="utf-8") == before and rs.rules_history() == []
