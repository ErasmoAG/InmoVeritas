"""Agente 4 - Extractor especializado de predial."""
from agents.extractor_base import COMMON_RULES, ExtractorAgent
from schemas.extraction import PredialOut


class ExtractorPredialAgent(ExtractorAgent):
    agent_id = "A4"
    name = "Extractor de predial"
    responsibility = "Extraer contribuyente, direccion, cuenta o clave predial, superficie registrada y ejercicio fiscal."
    doc_type = "predial"
    schema = PredialOut
    keywords = ["contribuyente", "propietario", "clave catastral", "cuenta", "predial", "superficie", "terreno", "ejercicio", "ano",
                "ubicacion", "domicilio", "direccion", "fecha de pago", "periodo", "importe", "folio"]
    system_prompt = (
        "Eres un especialista en recibos y boletas de impuesto predial municipal en Mexico. Extrae: propietario o contribuyente, "
        "direccion/ubicacion del inmueble, cuenta o clave predial/catastral, superficie registrada (terreno, m2), ejercicio fiscal o ano "
        "al que corresponde el pago (4 digitos; NO confundas con la fecha de impresion si el documento indica otro ejercicio), e importe o "
        "estado del pago. " + COMMON_RULES
    )
