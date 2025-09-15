from django.core.exceptions import ValidationError
from django.db import models
import re


def validate_pie_format(value):
    if not re.match(r'^PIE\d{3}[A-Z0-9]{5}\d{6}\d{2}$', value):
        raise ValidationError("PIE 코드 포맷이 올바르지 않습니다.")


# # 필드 수정 예시 (InventoryDiscrepancy 모델)
# pie_healthcare_num = models.CharField(
#     max_length=16,
#     validators=[validate_pie_format],  # Validator 추가
#     unique=True  # 중복 방지
# )
