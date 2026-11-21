import json
import os

import pytest

from services.encryption_service import (EncryptionError, EncryptionService, KeyDestroyed, MissingMasterKey,
                                         TamperedData, generate_master_key)
import base64


@pytest.fixture
def svc(tmp_path):
    return EncryptionService(storage_dir=tmp_path, master_key=os.urandom(32))


def test_roundtrip_y_no_hay_texto_plano_en_disco(svc, tmp_path):
    svc.create_expediente_key("EXP1")
    secreto = b"Propietario: Juan Perez 123456789"
    path = svc.put("EXP1", "original", "f1", secreto)
    assert svc.get("EXP1", "original", "f1") == secreto
    assert secreto not in path.read_bytes() and b"Juan" not in path.read_bytes()


def test_nonce_unico_por_operacion(svc):
    svc.create_expediente_key("EXP1")
    blobs = {svc.encrypt("EXP1", b"mismo contenido", "r") for _ in range(50)}
    assert len(blobs) == 50  # mismo texto, distinto nonce -> distinto cifrado


def test_claves_independientes_por_expediente(svc):
    svc.create_expediente_key("A"); svc.create_expediente_key("B")
    assert svc._dek("A") != svc._dek("B")
    blob = svc.encrypt("A", b"dato", "original/x")
    with pytest.raises(TamperedData):
        svc.decrypt("B", blob, "original/x")


def test_aad_impide_mover_archivo_entre_roles(svc):
    svc.create_expediente_key("A")
    blob = svc.encrypt("A", b"dato", "original/x")
    with pytest.raises(TamperedData):
        svc.decrypt("A", blob, "original/otro")


def test_alteracion_detectada(svc):
    svc.create_expediente_key("A")
    blob = bytearray(svc.encrypt("A", b"dato importante", "r"))
    blob[-1] ^= 0x01
    with pytest.raises(TamperedData):
        svc.decrypt("A", bytes(blob), "r")


def test_clave_separada_de_los_archivos(svc, tmp_path):
    svc.create_expediente_key("A")
    p = svc.put("A", "original", "f", b"x")
    assert svc.key_path("A").parent != p.parent
    assert "keys" in svc.key_path("A").parts and "expedientes" in p.parts
    meta = json.loads(svc.key_path("A").read_text())
    assert svc._dek("A") not in base64.b64decode(meta["wrapped"])  # la DEK va envuelta, no en claro


def test_destruir_clave_vuelve_irrecuperable(svc, tmp_path):
    svc.create_expediente_key("A")
    svc.put("A", "original", "f", b"contenido")
    assert svc.destroy_key("A") is True
    assert not svc.key_path("A").exists()
    # aunque el .enc siga en disco, ya no se puede descifrar
    svc2 = EncryptionService(storage_dir=tmp_path, master_key=svc._master)
    with pytest.raises(KeyDestroyed):
        svc2.get("A", "original", "f")
    with pytest.raises(KeyDestroyed):
        svc.get("A", "original", "f")  # tampoco desde la cache en memoria


def test_otra_clave_maestra_no_abre(svc, tmp_path):
    svc.create_expediente_key("A")
    otro = EncryptionService(storage_dir=tmp_path, master_key=os.urandom(32))
    with pytest.raises(TamperedData):
        otro._dek("A")


def test_json_roundtrip(svc):
    svc.create_expediente_key("A")
    svc.put_json("A", "resultado", "analisis", {"propietario": "Ñandú", "n": 3})
    assert svc.get_json("A", "resultado", "analisis") == {"propietario": "Ñandú", "n": 3}
    assert svc.list_names("A", "resultado") == ["analisis"]


def test_ids_peligrosos_rechazados(svc):
    with pytest.raises(EncryptionError):
        svc.create_expediente_key("../../etc")
    svc.create_expediente_key("A")
    with pytest.raises(EncryptionError):
        svc.put("A", "original", "../x", b"1")


def test_master_key_requerida(monkeypatch, tmp_path):
    monkeypatch.delenv("INMO_MASTER_KEY", raising=False)
    with pytest.raises(MissingMasterKey):
        EncryptionService(storage_dir=tmp_path)
    monkeypatch.setenv("INMO_MASTER_KEY", generate_master_key())
    EncryptionService(storage_dir=tmp_path)
    monkeypatch.setenv("INMO_MASTER_KEY", base64.b64encode(b"corta").decode())
    with pytest.raises(MissingMasterKey):
        EncryptionService(storage_dir=tmp_path)
