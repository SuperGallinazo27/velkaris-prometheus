from dataclasses import dataclass

from api.errors import AnalysisError
from api.schemas import AnalyzeRequest, DescriptorDataset


@dataclass(frozen=True)
class SourceConnector:
    source: str
    name: str
    authentication: str
    strategy: str

    def status(self):
        return dict(source=self.source, name=self.name, status="not_implemented",
                    authentication=self.authentication, strategy=self.strategy,
                    message="Contrato de integración; no realiza consultas ni entrega mediciones reales.")

    async def descriptors(self, request: AnalyzeRequest) -> DescriptorDataset:
        raise AnalysisError(503, "source_unavailable",
                            f"{self.name}: extracción científica no implementada/configurada. Se requiere un adaptador específico del producto y credenciales/licencia cuando corresponda. Use demo o descriptores externos verificados.")
