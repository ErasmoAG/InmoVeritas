"""Agente 6 - Extractor de Uso de Suelo, con evaluacion de compatibilidad por destino (tienda o CEDIS)."""
from agents.extractor_base import COMMON_RULES, ExtractorAgent
from schemas.extraction import UsoSueloOut

DEST = {
    "store": ("TIENDA COMERCIAL", "uso comercial, actividades comerciales autorizadas y restricciones que afecten la operacion de una tienda"),
    "cedis": ("CENTRO DE DISTRIBUCION (CEDIS)", "uso industrial, almacenamiento y bodegas, actividades logisticas y de distribucion, y restricciones que afecten un centro de distribucion"),
}


class ExtractorUsoSueloAgent(ExtractorAgent):
    agent_id = "A6"
    name = "Extractor de uso de suelo"
    responsibility = "Extraer uso autorizado, actividades, restricciones, superficie, fecha, vigencia y autoridad; evaluar compatibilidad con tienda o CEDIS."
    doc_type = "uso_suelo"
    schema = UsoSueloOut
    keywords = ["uso de suelo", "uso autorizado", "giro", "actividad", "actividades", "permitido", "permitidos", "restriccion", "condicion",
                "condicionante", "superficie", "vigencia", "vigente", "fecha", "autoridad", "comercial", "industrial", "bodega", "almacen",
                "habitacional", "servicios", "clave catastral", "ubicacion", "lote", "propietario"]
    human = ("DESTINO SOLICITADO: {dest_name}\nCRITERIO: evalua {dest_criteria}.\n"
             "TERMINOS DE BUSQUEDA PROVISIONALES (solo ayudan a localizar texto; NO son criterios juridicos): a favor={pos}; en contra={neg}.\n"
             "TEXTO DEL DOCUMENTO (con marcas de pagina):\n{text}")
    system_prompt = (
        "Eres un especialista en licencias, constancias y dictamenes de uso de suelo municipales en Mexico. Extrae: direccion, identificacion "
        "del predio, uso de suelo autorizado o giro, actividades permitidas, restricciones y condicionantes, superficie aplicable, fecha de "
        "emision, vigencia (solo si el documento la declara), autoridad emisora, y propietario/solicitante si aparece. Ademas evalua la "
        "compatibilidad (compatible, incompatible, indeterminado) del uso autorizado con el DESTINO SOLICITADO usando UNICAMENTE lo que dice "
        "el documento. No asumas que cualquier uso industrial sirve para una tienda ni que cualquier uso comercial sirve para un CEDIS. "
        "Si el documento no permite concluir, responde indeterminado. " + COMMON_RULES
    )

    def extra_vars(self, project_type=None, rules=None, **kw) -> dict:
        key = getattr(project_type, "value", project_type)
        name, crit = DEST.get(key, ("NO ESPECIFICADO", "el uso autorizado sin asumir un destino; responde indeterminado en compatibilidad"))
        hints = {"positive": [], "negative": []}
        if rules is not None and key in ("store", "cedis"):
            h = getattr(rules.rules.land_use, key).provisional_hints
            hints = {"positive": h.positive, "negative": h.negative}
        return {"dest_name": name, "dest_criteria": crit, "pos": ", ".join(hints["positive"]) or "ninguno", "neg": ", ".join(hints["negative"]) or "ninguno"}
