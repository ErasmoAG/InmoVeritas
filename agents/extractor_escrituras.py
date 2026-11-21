"""Agente 3 - Extractor especializado de escrituras."""
from agents.extractor_base import COMMON_RULES, ExtractorAgent
from schemas.extraction import EscrituraOut


class ExtractorEscriturasAgent(ExtractorAgent):
    agent_id = "A3"
    name = "Extractor de escrituras"
    responsibility = "Extraer titular, direccion e identificacion del inmueble, superficie del terreno y datos que lo relacionan con otros documentos."
    doc_type = "escrituras"
    schema = EscrituraOut
    keywords = ["propietario", "titular", "adquirente", "comprador", "vendedor", "enajenante", "superficie", "metros cuadrados", "m2",
                "ubicado", "ubicacion", "lote", "manzana", "calle", "colonia", "clave catastral", "folio real", "escritura numero",
                "instrumento", "notario", "notaria", "colindancias", "medidas"]
    system_prompt = (
        "Eres un abogado especialista en escrituras publicas de inmuebles en Mexico. Del texto de una escritura extrae: propietario o "
        "titular (el adquirente o dueño actual, nombre completo), direccion/ubicacion del inmueble, identificacion del predio (lote, "
        "manzana, clave catastral, folio real), superficie del terreno en metros cuadrados, numero de escritura y fecha, notario, y otros "
        "datos relevantes (medidas y colindancias, gravamenes, antecedentes). " + COMMON_RULES
    )
