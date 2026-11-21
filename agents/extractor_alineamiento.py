"""Agente 5 - Extractor de Alineamiento y Numero Oficial."""
from agents.extractor_base import COMMON_RULES, ExtractorAgent
from schemas.extraction import AlineamientoOut


class ExtractorAlineamientoAgent(ExtractorAgent):
    agent_id = "A5"
    name = "Extractor de alineamiento y número oficial"
    responsibility = "Extraer direccion oficial, numero oficial, identificacion del predio, alineamiento, fecha de emision, vigencia y autoridad."
    doc_type = "alineamiento_numero_oficial"
    schema = AlineamientoOut
    keywords = ["numero oficial", "alineamiento", "direccion", "ubicacion", "clave catastral", "lote", "manzana", "fecha", "vigencia",
                "vigente", "autoridad", "direccion de desarrollo urbano", "propietario", "solicitante", "superficie", "restriccion", "afectacion"]
    system_prompt = (
        "Eres un especialista en constancias de alineamiento y numero oficial emitidas por autoridades municipales de desarrollo urbano en "
        "Mexico. Extrae: direccion oficial, numero oficial asignado, identificacion del predio (clave catastral, lote, manzana), "
        "alineamiento determinado por la autoridad (afectaciones, restricciones, derechos de via), fecha de emision, vigencia (solo si el "
        "documento la declara), autoridad emisora, y propietario/solicitante y superficie si aparecen. "
        "Pistas de donde buscar: la fecha de emision suele rotularse 'FECHA DE EXPEDICION' o 'fecha'; el numero oficial aparece tras 'NUMERO OFICIAL'; "
        "la direccion tras 'inmueble ubicado' o 'ubicado en'; la clave tras 'clave catastral'; el propietario tras 'a favor de'; la autoridad es la "
        "Direccion de Desarrollo Urbano / municipio del encabezado; el alineamiento describe medidas, colindancias, restricciones o afectaciones del "
        "predio con la via publica. Responde TODOS los campos que encuentres en el texto. " + COMMON_RULES
    )
