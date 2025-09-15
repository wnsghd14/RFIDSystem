from core.logger import logger
from inventory2.backend.models.inventory import Inventory2


class QueryParamFilterMixin:
    """
    company, type, date를 기반으로 공통 queryset 필터링하는 Mixin
    """
    def get_queryset(self):
        queryset = super().get_queryset()  #  올바르게 부모 호출

        company_name = self.request.query_params.get("company")
        type_name = self.request.query_params.get("type")
        date = self.request.query_params.get("date")
        logger.info(f"company_id: {company_name}, type_name: {type_name}, date: {date}")
        if company_name:
            queryset = queryset.filter(date__company__id=company_name)
        if type_name:
            queryset = queryset.filter(date__type__id=type_name)
        if date:
            queryset = queryset.filter(date__date=date)

        return queryset