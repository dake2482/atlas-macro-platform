from __future__ import annotations

import uuid
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, URLValidator
from django.db import models
from django.urls import reverse


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Source(TimestampedModel):
    class LicenseStatus(models.TextChoices):
        OPEN = "open", "开放"
        REVIEW = "review", "待审核"
        LICENSED = "licensed", "已授权"
        RESTRICTED = "restricted", "受限制"

    key = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=160)
    homepage = models.URLField(blank=True)
    kind = models.CharField(max_length=60, default="official")
    license_status = models.CharField(
        max_length=20, choices=LicenseStatus.choices, default=LicenseStatus.REVIEW
    )
    license_scope = models.TextField(blank=True)
    redistribution_allowed = models.BooleanField(default=False)
    attribution = models.CharField(max_length=240, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class SourceLicense(TimestampedModel):
    """Versioned licence decision for an upstream source.

    Keeping this separate from ``Source`` preserves the review history when a
    provider changes terms or a commercial redistribution agreement expires.
    """

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="licenses")
    is_current = models.BooleanField(default=True, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=Source.LicenseStatus.choices,
        default=Source.LicenseStatus.REVIEW,
    )
    scope = models.TextField()
    required_notice = models.TextField(blank=True)
    terms_url = models.URLField(max_length=800, blank=True)
    redistribution_allowed = models.BooleanField(default=False)
    public_display_allowed = models.BooleanField(default=False)
    derived_display_allowed = models.BooleanField(default=False)
    historical_storage_allowed = models.BooleanField(default=False)
    ai_use_allowed = models.BooleanField(default=False)
    territories = models.CharField(
        max_length=240,
        blank=True,
        help_text="Contracted display territories; blank means not confirmed.",
    )
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    reviewed_by = models.CharField(max_length=160, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        # ``reviewed_at`` is intentionally not the primary sort key: on
        # PostgreSQL a descending nullable column sorts NULL values first,
        # which can make an unreviewed historical row look like the latest
        # licence decision. ``created_at`` is always populated and ``pk``
        # makes ties deterministic.
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["source"],
                condition=models.Q(is_current=True),
                name="one_current_license_per_source",
            )
        ]


class DataRequirement(TimestampedModel):
    """Page-level data coverage contract and procurement backlog."""

    class Status(models.TextChoices):
        LIVE = "live", "已接入"
        PROXY = "proxy", "代理指标"
        NEEDS_SOURCE = "needs_source", "待找数据源"
        LICENSE_REVIEW = "license_review", "许可待审核"
        PURCHASE_REQUIRED = "purchase_required", "需采购"

    key = models.SlugField(max_length=160, unique=True)
    page_key = models.SlugField(max_length=120, db_index=True)
    metric_name = models.CharField(max_length=180)
    status = models.CharField(max_length=24, choices=Status.choices)
    source_name = models.CharField(max_length=180, blank=True)
    source_url = models.URLField(max_length=800, blank=True)
    vendor = models.CharField(max_length=180, blank=True)
    product = models.CharField(max_length=240, blank=True)
    reason = models.TextField(blank=True)
    proxy_description = models.TextField(blank=True)
    priority = models.PositiveSmallIntegerField(default=5)
    last_verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["priority", "page_key", "metric_name"]


class IngestionRun(TimestampedModel):
    class Status(models.TextChoices):
        RUNNING = "running", "运行中"
        SUCCESS = "success", "成功"
        FAILED = "failed", "失败"
        PARTIAL = "partial", "部分成功"

    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="runs")
    dataset = models.CharField(max_length=120)
    batch_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    row_count = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-started_at"]


class RawArtifact(TimestampedModel):
    run = models.ForeignKey(IngestionRun, on_delete=models.CASCADE, related_name="artifacts")
    uri = models.CharField(max_length=500)
    sha256 = models.CharField(max_length=64)
    content_type = models.CharField(max_length=120, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run", "sha256"], name="raw_artifact_run_sha256"
            )
        ]
        indexes = [models.Index(fields=["sha256"])]


class Instrument(TimestampedModel):
    symbol = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=160)
    asset_class = models.CharField(max_length=40)
    exchange = models.CharField(max_length=80, blank=True)
    currency = models.CharField(max_length=12, default="USD")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["asset_class", "symbol"]

    def __str__(self) -> str:
        return f"{self.symbol} · {self.name}"


class SeriesDefinition(TimestampedModel):
    key = models.SlugField(max_length=120, unique=True)
    name = models.CharField(max_length=180)
    unit = models.CharField(max_length=40, blank=True)
    frequency = models.CharField(max_length=30, default="daily")
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="series")
    description = models.TextField(blank=True)

    def __str__(self) -> str:
        return self.name


class Observation(TimestampedModel):
    class Quality(models.TextChoices):
        FRESH = "fresh", "正常"
        STALE = "stale", "过期"
        FALLBACK = "fallback", "备用源"
        ESTIMATED = "estimated", "估算"
        ERROR = "error", "异常"

    series = models.ForeignKey(
        SeriesDefinition,
        on_delete=models.CASCADE,
        related_name="observations",
        null=True,
        blank=True,
    )
    instrument = models.ForeignKey(
        Instrument, on_delete=models.CASCADE, related_name="observations", null=True, blank=True
    )
    value = models.DecimalField(max_digits=28, decimal_places=8)
    value_date = models.DateTimeField()
    as_of = models.DateTimeField()
    fetched_at = models.DateTimeField()
    batch_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="observations")
    fallback_source = models.ForeignKey(
        Source,
        on_delete=models.PROTECT,
        related_name="fallback_observations",
        null=True,
        blank=True,
    )
    quality_status = models.CharField(max_length=20, choices=Quality.choices, default=Quality.FRESH)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["instrument", "-value_date"]),
            models.Index(fields=["series", "-value_date"]),
            models.Index(fields=["batch_id"]),
        ]
        ordering = ["-value_date"]


class ReleaseVintageObservation(TimestampedModel):
    """A value as published in one identifiable official release vintage."""

    series = models.ForeignKey(
        SeriesDefinition,
        on_delete=models.CASCADE,
        related_name="release_vintages",
    )
    value = models.DecimalField(max_digits=28, decimal_places=8)
    value_date = models.DateTimeField()
    as_of = models.DateTimeField()
    release_date = models.DateField()
    estimate_round = models.CharField(max_length=60)
    vintage_label = models.CharField(max_length=120)
    fetched_at = models.DateTimeField()
    batch_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    source = models.ForeignKey(
        Source,
        on_delete=models.PROTECT,
        related_name="release_vintage_observations",
    )
    fallback_source = models.ForeignKey(
        Source,
        on_delete=models.PROTECT,
        related_name="fallback_release_vintage_observations",
        null=True,
        blank=True,
    )
    quality_status = models.CharField(
        max_length=20,
        choices=Observation.Quality.choices,
        default=Observation.Quality.FRESH,
    )
    license_scope = models.CharField(max_length=240, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-value_date", "release_date", "estimate_round", "series"]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "series",
                    "value_date",
                    "release_date",
                    "estimate_round",
                    "source",
                ],
                name="release_vintage_series_period_round_source",
            )
        ]
        indexes = [
            models.Index(fields=["series", "-value_date", "release_date"]),
            models.Index(fields=["source", "batch_id"]),
        ]


class MarketBar(TimestampedModel):
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name="bars")
    interval = models.CharField(max_length=20, default="1d")
    value_date = models.DateTimeField()
    open = models.DecimalField(max_digits=28, decimal_places=8)
    high = models.DecimalField(max_digits=28, decimal_places=8)
    low = models.DecimalField(max_digits=28, decimal_places=8)
    close = models.DecimalField(max_digits=28, decimal_places=8)
    volume = models.DecimalField(max_digits=30, decimal_places=4, null=True, blank=True)
    fetched_at = models.DateTimeField()
    batch_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="market_bars")
    fallback_source = models.ForeignKey(
        Source,
        on_delete=models.PROTECT,
        related_name="fallback_market_bars",
        null=True,
        blank=True,
    )
    quality_status = models.CharField(
        max_length=20, choices=Observation.Quality.choices, default=Observation.Quality.FRESH
    )
    license_scope = models.CharField(max_length=240, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-value_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["instrument", "interval", "value_date", "source"],
                name="market_bar_source_interval_time",
            )
        ]
        indexes = [models.Index(fields=["instrument", "interval", "-value_date"])]


class MetricSnapshot(TimestampedModel):
    key = models.SlugField(max_length=140)
    label = models.CharField(max_length=180)
    value = models.DecimalField(max_digits=28, decimal_places=8, null=True, blank=True)
    display_value = models.CharField(max_length=80, blank=True)
    change = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    unit = models.CharField(max_length=30, blank=True)
    value_date = models.DateTimeField()
    as_of = models.DateTimeField()
    fetched_at = models.DateTimeField()
    batch_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    source = models.ForeignKey(Source, on_delete=models.PROTECT)
    fallback_source = models.ForeignKey(
        Source,
        on_delete=models.PROTECT,
        related_name="fallback_metrics",
        null=True,
        blank=True,
    )
    quality_status = models.CharField(
        max_length=20, choices=Observation.Quality.choices, default=Observation.Quality.FRESH
    )
    license_scope = models.CharField(max_length=120, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["key", "batch_id"], name="metric_batch_key")]
        ordering = ["key"]


class DashboardSnapshot(TimestampedModel):
    key = models.SlugField(max_length=120)
    title = models.CharField(max_length=180)
    as_of = models.DateTimeField()
    batch_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    quality_status = models.CharField(
        max_length=20, choices=Observation.Quality.choices, default=Observation.Quality.FRESH
    )
    summary = models.TextField(blank=True)
    data = models.JSONField(default=dict)
    source = models.ForeignKey(Source, on_delete=models.PROTECT)
    is_published = models.BooleanField(default=False)

    class Meta:
        ordering = ["-as_of"]
        constraints = [
            models.UniqueConstraint(fields=["key", "batch_id"], name="dashboard_batch_key")
        ]


class QualityCheck(TimestampedModel):
    class Status(models.TextChoices):
        PASS = "pass", "通过"
        WARN = "warn", "警告"
        FAIL = "fail", "失败"

    run = models.ForeignKey(
        IngestionRun,
        on_delete=models.CASCADE,
        related_name="quality_checks",
        null=True,
        blank=True,
    )
    batch_id = models.UUIDField(db_index=True)
    scope_key = models.CharField(max_length=160, db_index=True)
    check_name = models.CharField(max_length=160)
    status = models.CharField(max_length=12, choices=Status.choices)
    observed_at = models.DateTimeField()
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-observed_at", "scope_key"]


class FallbackEvent(TimestampedModel):
    dataset = models.CharField(max_length=160, db_index=True)
    primary_source = models.ForeignKey(
        Source, on_delete=models.PROTECT, related_name="primary_fallback_events"
    )
    fallback_source = models.ForeignKey(
        Source, on_delete=models.PROTECT, related_name="activated_fallback_events"
    )
    batch_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    reason = models.TextField()
    began_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    resolved = models.BooleanField(default=False)

    class Meta:
        ordering = ["-began_at"]


class GeneratedAnalysis(TimestampedModel):
    class ReviewStatus(models.TextChoices):
        DRAFT = "draft", "草稿"
        AI = "ai", "AI 生成"
        REVIEWED = "reviewed", "已审核"
        REJECTED = "rejected", "已拒绝"

    slug = models.SlugField(max_length=180, unique=True)
    title = models.CharField(max_length=240)
    body = models.TextField()
    model_name = models.CharField(max_length=120, blank=True)
    prompt_version = models.CharField(max_length=80, blank=True)
    generated_at = models.DateTimeField()
    review_status = models.CharField(
        max_length=20, choices=ReviewStatus.choices, default=ReviewStatus.DRAFT
    )
    evidence = models.JSONField(default=list, blank=True)
    data_as_of = models.DateTimeField(null=True, blank=True)
    stale = models.BooleanField(default=False)


class Thesis(TimestampedModel):
    class ReviewStatus(models.TextChoices):
        DRAFT = "draft", "草稿"
        REVIEWED = "reviewed", "已审核"
        REJECTED = "rejected", "已拒绝"

    class Status(models.TextChoices):
        PENDING = "pending", "待复盘"
        HIT = "hit", "命中"
        PARTIAL = "partial", "部分命中"
        MISSED = "missed", "未命中"

    date = models.DateField(unique=True)
    regime = models.CharField(max_length=80)
    confidence = models.CharField(max_length=20, default="中")
    summary = models.TextField()
    evidence = models.JSONField(default=list)
    triggers = models.JSONField(default=list)
    invalidation = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    hit_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    simulated_return = models.DecimalField(max_digits=7, decimal_places=3, null=True, blank=True)
    review_status = models.CharField(
        max_length=20,
        choices=ReviewStatus.choices,
        default=ReviewStatus.DRAFT,
        db_index=True,
    )
    reviewed_by = models.CharField(max_length=160, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    publication_fingerprint = models.CharField(max_length=64, blank=True)
    is_published = models.BooleanField(default=False, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True)
    source_snapshot = models.ForeignKey(
        DashboardSnapshot,
        on_delete=models.PROTECT,
        related_name="published_theses",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-date"]
        permissions = [
            ("publish_thesis", "Can review and publish thesis"),
            ("withdraw_thesis", "Can withdraw published thesis"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(is_published=False, published_at__isnull=True)
                    | (
                        models.Q(
                            is_published=True,
                            published_at__isnull=False,
                            review_status="reviewed",
                            reviewed_at__isnull=False,
                            source_snapshot__isnull=False,
                        )
                        & ~models.Q(reviewed_by="")
                        & ~models.Q(publication_fingerprint="")
                    )
                ),
                name="thesis_publication_review_state_consistent",
            )
        ]

    def get_absolute_url(self) -> str:
        return reverse("daily-detail", kwargs={"report_date": self.date.isoformat()})


class EvidenceItem(TimestampedModel):
    analysis = models.ForeignKey(
        GeneratedAnalysis,
        on_delete=models.CASCADE,
        related_name="evidence_items",
        null=True,
        blank=True,
    )
    thesis = models.ForeignKey(
        Thesis,
        on_delete=models.CASCADE,
        related_name="evidence_items",
        null=True,
        blank=True,
    )
    label = models.CharField(max_length=180)
    body = models.TextField()
    source = models.ForeignKey(Source, on_delete=models.PROTECT, null=True, blank=True)
    source_url = models.URLField(max_length=800, blank=True)
    observation = models.ForeignKey(Observation, on_delete=models.SET_NULL, null=True, blank=True)
    snapshot = models.ForeignKey(MetricSnapshot, on_delete=models.SET_NULL, null=True, blank=True)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    value_date = models.DateTimeField(null=True, blank=True)


class Trigger(TimestampedModel):
    class Status(models.TextChoices):
        WATCHING = "watching", "观察中"
        TRIGGERED = "triggered", "已触发"
        EXPIRED = "expired", "已过期"

    thesis = models.ForeignKey(Thesis, on_delete=models.CASCADE, related_name="trigger_items")
    name = models.CharField(max_length=180)
    condition = models.TextField()
    display_threshold = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.WATCHING)
    triggered_at = models.DateTimeField(null=True, blank=True)


class Invalidation(TimestampedModel):
    thesis = models.OneToOneField(
        Thesis, on_delete=models.CASCADE, related_name="invalidation_record"
    )
    condition = models.TextField()
    is_triggered = models.BooleanField(default=False)
    observed_at = models.DateTimeField(null=True, blank=True)
    evidence = models.JSONField(default=list, blank=True)


class Outcome(TimestampedModel):
    thesis = models.OneToOneField(Thesis, on_delete=models.CASCADE, related_name="outcome")
    evaluated_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Thesis.Status.choices)
    hit_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    simulated_return = models.DecimalField(max_digits=7, decimal_places=3, null=True, blank=True)
    notes = models.TextField(blank=True)


class NewsItem(TimestampedModel):
    title = models.CharField(max_length=320)
    original_title = models.CharField(max_length=320, blank=True)
    summary = models.TextField(blank=True)
    source_name = models.CharField(max_length=120)
    source_url = models.URLField(max_length=800)
    category = models.CharField(max_length=80, db_index=True)
    published_at = models.DateTimeField(db_index=True)
    tickers = models.JSONField(default=list, blank=True)
    themes = models.JSONField(default=list, blank=True)
    sentiment = models.CharField(max_length=40, blank=True)
    relevance = models.PositiveSmallIntegerField(default=0)
    license_status = models.CharField(max_length=20, default="link-only")

    class Meta:
        ordering = ["-published_at"]


class ResearchMention(TimestampedModel):
    bank = models.CharField(max_length=120, db_index=True)
    title = models.CharField(max_length=320)
    summary = models.TextField(blank=True)
    category = models.CharField(max_length=80, db_index=True)
    stance = models.CharField(max_length=30, blank=True)
    importance = models.PositiveSmallIntegerField(default=5)
    published_at = models.DateTimeField()
    source_url = models.URLField(max_length=800)
    review_status = models.CharField(max_length=20, default="ai")

    class Meta:
        ordering = ["-published_at"]


class FundLetter(TimestampedModel):
    fund_name = models.CharField(max_length=180, db_index=True)
    fund_name_en = models.CharField(max_length=180, blank=True)
    manager = models.CharField(max_length=180, blank=True)
    quarter = models.CharField(max_length=30, db_index=True)
    strategy = models.CharField(max_length=60, db_index=True)
    stance = models.CharField(max_length=30, db_index=True)
    aum_usd_m = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    summary = models.TextField()
    key_points = models.JSONField(default=list)
    asset_views = models.JSONField(default=list, blank=True)
    original_url = models.URLField(max_length=800)
    source_label = models.CharField(max_length=120, default="基金官网")
    license_status = models.CharField(max_length=20, default="link-only")
    published_at = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-published_at", "fund_name"]

    def get_absolute_url(self) -> str:
        return reverse("fund-letter-detail", kwargs={"pk": self.pk})


class FedDocument(TimestampedModel):
    class DocumentType(models.TextChoices):
        STATEMENT = "statement", "FOMC 声明"
        SPEECH = "speech", "官员演讲"
        NEWS = "news", "联储公告"

    class AnalysisStatus(models.TextChoices):
        DRAFT = "draft", "待分析 / 待审核"
        AI_GENERATED = "ai_generated", "AI 已生成（未人工审核）"
        REVIEWED = "reviewed", "已人工审核"
        REJECTED = "rejected", "已拒绝"

    document_type = models.CharField(max_length=20, choices=DocumentType.choices, db_index=True)
    slug = models.SlugField(max_length=180, unique=True)
    title = models.CharField(max_length=320)
    speaker = models.CharField(max_length=120, blank=True)
    official_description = models.TextField(blank=True)
    summary = models.TextField(blank=True)
    key_points = models.JSONField(default=list)
    published_at = models.DateTimeField()
    hawkish_score = models.SmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(-5), MaxValueValidator(5)],
    )
    original_url = models.URLField(max_length=800)
    analysis_status = models.CharField(
        max_length=20,
        choices=AnalysisStatus.choices,
        default=AnalysisStatus.DRAFT,
        db_index=True,
    )
    analysis_model = models.CharField(max_length=160, blank=True)
    analysis_prompt_version = models.CharField(max_length=80, blank=True)
    analysis_generated_at = models.DateTimeField(null=True, blank=True)
    analysis_evidence = models.JSONField(default=list, blank=True)
    reviewed_by = models.CharField(max_length=160, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-published_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(hawkish_score__isnull=True)
                    | models.Q(hawkish_score__gte=-5, hawkish_score__lte=5)
                ),
                name="fed_hawkish_score_range",
            )
        ]

    @staticmethod
    def _is_safe_evidence_url(value) -> bool:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            return False
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
        except ValueError:
            return False
        if (
            parsed.scheme.lower() != "https"
            or not parsed.netloc
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False
        try:
            URLValidator(schemes=["https"])(value)
        except ValidationError:
            return False
        return True

    @classmethod
    def _is_valid_evidence_item(cls, item) -> bool:
        if not isinstance(item, dict):
            return False
        has_stable_identifier = any(
            isinstance(item.get(key), str)
            and bool(item[key].strip())
            and item[key] == item[key].strip()
            for key in ("id", "source_id")
        )
        if not has_stable_identifier:
            return False
        if "url" in item and not cls._is_safe_evidence_url(item["url"]):
            return False
        return True

    @classmethod
    def _is_valid_evidence_list(cls, evidence) -> bool:
        return bool(
            isinstance(evidence, list)
            and evidence
            and all(cls._is_valid_evidence_item(item) for item in evidence)
        )

    @property
    def analysis_provenance_complete(self) -> bool:
        return bool(
            self.summary.strip()
            and self.analysis_model.strip()
            and self.analysis_prompt_version.strip()
            and self.analysis_generated_at
            and self._is_valid_evidence_list(self.analysis_evidence)
        )

    @property
    def has_public_analysis(self) -> bool:
        if self.analysis_status not in {
            self.AnalysisStatus.AI_GENERATED,
            self.AnalysisStatus.REVIEWED,
        }:
            return False
        if not self.analysis_provenance_complete:
            return False
        if self.analysis_status == self.AnalysisStatus.REVIEWED:
            return bool(self.reviewed_by.strip() and self.reviewed_at)
        return True

    @property
    def has_public_score(self) -> bool:
        return self.has_public_analysis and self.hawkish_score is not None

    @property
    def public_analysis_evidence(self) -> list[dict]:
        if not self.has_public_analysis:
            return []
        return self.analysis_evidence

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        public_statuses = {
            self.AnalysisStatus.AI_GENERATED,
            self.AnalysisStatus.REVIEWED,
        }
        if self.analysis_status in public_statuses:
            required = {
                "summary": self.summary.strip(),
                "analysis_model": self.analysis_model.strip(),
                "analysis_prompt_version": self.analysis_prompt_version.strip(),
                "analysis_generated_at": self.analysis_generated_at,
                "analysis_evidence": self.analysis_evidence,
            }
            for field, value in required.items():
                if not value:
                    errors[field] = "AI 已生成或已审核状态必须提供完整分析来源。"
            if not self._is_valid_evidence_list(self.analysis_evidence):
                errors["analysis_evidence"] = (
                    "证据必须是非空对象列表；每项需含非空 id/source_id，"
                    "可选链接只能使用无凭据的绝对 HTTPS URL。"
                )
        if self.analysis_status in {
            self.AnalysisStatus.REVIEWED,
            self.AnalysisStatus.REJECTED,
        }:
            if not self.reviewed_by.strip():
                errors["reviewed_by"] = "审核或拒绝状态必须记录审核人。"
            if self.reviewed_at is None:
                errors["reviewed_at"] = "审核或拒绝状态必须记录审核时间。"
        if errors:
            raise ValidationError(errors)


class SupplyChainNode(TimestampedModel):
    slug = models.SlugField(max_length=120, unique=True)
    name = models.CharField(max_length=160)
    layer = models.CharField(max_length=80, db_index=True)
    description = models.TextField()
    thesis = models.TextField(blank=True)
    quadrant = models.CharField(max_length=40, default="观察", db_index=True)
    narrative_score = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    revenue_growth = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    gross_margin = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    median_pe = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    median_ps = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    market_cap_usd_m = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    source_note = models.CharField(max_length=240, blank=True)

    class Meta:
        ordering = ["layer", "name"]

    def get_absolute_url(self) -> str:
        return reverse("ai-node", kwargs={"slug": self.slug})


class Company(TimestampedModel):
    slug = models.SlugField(max_length=140, unique=True)
    name = models.CharField(max_length=180)
    name_en = models.CharField(max_length=180, blank=True)
    ticker = models.CharField(max_length=40, db_index=True)
    exchange = models.CharField(max_length=80, blank=True)
    country = models.CharField(max_length=80, blank=True)
    currency = models.CharField(max_length=12, default="USD")
    primary_node = models.ForeignKey(
        SupplyChainNode, on_delete=models.PROTECT, related_name="companies"
    )
    description = models.TextField()
    business = models.TextField(blank=True)
    price = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    market_cap_usd_m = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    return_1m = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    return_6m = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    revenue_growth = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    gross_margin = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    pe = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    ps = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    rating = models.CharField(max_length=40, default="", blank=True)
    quality_grade = models.CharField(max_length=10, default="", blank=True)
    data_source_note = models.CharField(max_length=240, blank=True)
    investor_relations_url = models.URLField(max_length=800, blank=True)
    data_as_of = models.DateField(null=True, blank=True)
    sec_cik = models.CharField(max_length=10, blank=True, db_index=True)
    source = models.ForeignKey(
        Source, on_delete=models.PROTECT, related_name="companies", null=True, blank=True
    )
    fallback_source = models.ForeignKey(
        Source,
        on_delete=models.PROTECT,
        related_name="fallback_companies",
        null=True,
        blank=True,
    )
    publication_batch_id = models.UUIDField(null=True, blank=True, db_index=True)
    fetched_at = models.DateTimeField(null=True, blank=True)
    quality_status = models.CharField(
        max_length=20, choices=Observation.Quality.choices, default=Observation.Quality.ERROR
    )
    license_scope = models.CharField(max_length=240, blank=True)
    is_published = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["sec_cik"],
                condition=~models.Q(sec_cik=""),
                name="company_nonblank_sec_cik_unique",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(is_published=False)
                    | (
                        models.Q(source__isnull=False)
                        & models.Q(publication_batch_id__isnull=False)
                        & models.Q(fetched_at__isnull=False)
                        & ~models.Q(license_scope="")
                        & models.Q(fallback_source__isnull=True)
                        & ~models.Q(quality_status=Observation.Quality.ERROR)
                    )
                ),
                name="published_company_publication_contract",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(is_published=False)
                    | (
                        ~models.Q(slug__in=("microsoft", "alphabet", "amazon", "meta"))
                        & ~models.Q(sec_cik__in=("0000789019", "0001652044", "0001018724", "0001326801"))
                    )
                    | models.Q(slug="microsoft", sec_cik="0000789019")
                    | models.Q(slug="alphabet", sec_cik="0001652044")
                    | models.Q(slug="amazon", sec_cik="0001018724")
                    | models.Q(slug="meta", sec_cik="0001326801")
                ),
                name="published_reviewed_company_identity",
            ),
        ]

    def get_absolute_url(self) -> str:
        return reverse("ai-company", kwargs={"slug": self.slug})


class FinancialFact(TimestampedModel):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="financials")
    fiscal_year = models.PositiveSmallIntegerField()
    revenue_usd_m = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    revenue_growth = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    gross_margin = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    net_income_usd_m = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    operating_cash_flow_usd_m = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True
    )
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    fiscal_period = models.CharField(max_length=12, blank=True)
    gross_profit_usd_m = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    capital_expenditures_usd_m = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True
    )
    capex_intensity = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    capex_definition = models.CharField(max_length=80, blank=True)
    capex_source_url = models.URLField(max_length=800, blank=True)
    capex_source_fact = models.ForeignKey(
        "SECCompanyFact",
        on_delete=models.PROTECT,
        related_name="financial_projections",
        null=True,
        blank=True,
    )
    accession_number = models.CharField(max_length=40, blank=True)
    form = models.CharField(max_length=20, blank=True)
    source_url = models.URLField(max_length=800, blank=True)
    publication_batch_id = models.UUIDField(null=True, blank=True, db_index=True)
    fetched_at = models.DateTimeField(null=True, blank=True)
    quality_status = models.CharField(
        max_length=20, choices=Observation.Quality.choices, default=Observation.Quality.ERROR
    )
    license_scope = models.CharField(max_length=240, blank=True)
    fallback_source = models.ForeignKey(
        Source,
        on_delete=models.PROTECT,
        related_name="fallback_financial_facts",
        null=True,
        blank=True,
    )
    metadata = models.JSONField(default=dict, blank=True)
    source = models.ForeignKey(Source, on_delete=models.PROTECT)
    filed_at = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-fiscal_year"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "fiscal_year"],
                condition=models.Q(publication_batch_id__isnull=True),
                name="company_fiscal_year_unbatched_unique",
            ),
            models.UniqueConstraint(
                fields=["company", "fiscal_year", "publication_batch_id"],
                name="company_fiscal_year_publication_batch",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(publication_batch_id__isnull=True)
                    | (
                        models.Q(period_start__isnull=False)
                        & models.Q(period_end__isnull=False)
                        & models.Q(capital_expenditures_usd_m__isnull=False)
                        & models.Q(capex_source_fact__isnull=False)
                        & models.Q(fetched_at__isnull=False)
                        & ~models.Q(license_scope="")
                        & models.Q(fallback_source__isnull=True)
                        & models.Q(capital_expenditures_usd_m__gte=0)
                    )
                ),
                name="published_financial_fact_contract",
            ),
        ]


class SECCompanyFact(TimestampedModel):
    """Immutable, narrow SEC XBRL fact retained before publication projection."""

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="sec_facts")
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="sec_company_facts")
    source_license = models.ForeignKey(
        SourceLicense, on_delete=models.PROTECT, related_name="sec_company_facts"
    )
    ingestion_run = models.ForeignKey(
        IngestionRun, on_delete=models.PROTECT, related_name="sec_company_facts"
    )
    raw_artifact = models.ForeignKey(
        RawArtifact, on_delete=models.PROTECT, related_name="sec_company_facts"
    )
    taxonomy = models.CharField(max_length=40)
    concept = models.CharField(max_length=180)
    unit = models.CharField(max_length=40)
    value = models.DecimalField(max_digits=28, decimal_places=4)
    period_start = models.DateField()
    period_end = models.DateField()
    fiscal_year = models.PositiveSmallIntegerField()
    fiscal_period = models.CharField(max_length=12, default="FY")
    form = models.CharField(max_length=20)
    filed_at = models.DateField()
    accession_number = models.CharField(max_length=40)
    frame = models.CharField(max_length=40, blank=True)
    fetched_at = models.DateTimeField()
    quality_status = models.CharField(
        max_length=20, choices=Observation.Quality.choices, default=Observation.Quality.FRESH
    )
    license_scope = models.CharField(max_length=240)
    fallback_source = models.ForeignKey(
        Source,
        on_delete=models.PROTECT,
        related_name="fallback_sec_company_facts",
        null=True,
        blank=True,
    )
    identity_hash = models.CharField(max_length=64, unique=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["company", "fiscal_year", "concept", "filed_at", "accession_number"]
        indexes = [
            models.Index(fields=["company", "concept", "period_end"]),
            models.Index(fields=["ingestion_run", "concept"]),
            models.Index(fields=["accession_number"]),
        ]


class SupplyChainEdge(TimestampedModel):
    source_node = models.ForeignKey(
        SupplyChainNode, on_delete=models.CASCADE, related_name="outbound_edges"
    )
    target_node = models.ForeignKey(
        SupplyChainNode, on_delete=models.CASCADE, related_name="inbound_edges"
    )
    relation = models.CharField(max_length=120)
    confidence = models.DecimalField(max_digits=4, decimal_places=2, default=0.7)
    evidence_url = models.URLField(max_length=800, blank=True)
    reviewed = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_node", "target_node", "relation"], name="unique_supply_edge"
            )
        ]


class ModelProfile(TimestampedModel):
    slug = models.SlugField(max_length=120, unique=True)
    name = models.CharField(max_length=160)
    provider = models.CharField(max_length=120)
    release_date = models.DateField()
    context_tokens = models.PositiveIntegerField(default=0)
    input_price = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    output_price = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    capability_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    tier = models.CharField(max_length=10, default="T1")
    description = models.TextField()
    sources = models.JSONField(default=list)

    class Meta:
        ordering = ["-capability_score"]

    def get_absolute_url(self) -> str:
        return reverse("model-detail", kwargs={"slug": self.slug})


class CodingAgentProfile(TimestampedModel):
    slug = models.SlugField(max_length=120, unique=True)
    name = models.CharField(max_length=160)
    provider = models.CharField(max_length=120)
    product_type = models.CharField(max_length=80)
    release_date = models.DateField(null=True, blank=True)
    price_label = models.CharField(max_length=120)
    capability_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    description = models.TextField()
    homepage = models.URLField(blank=True)

    class Meta:
        ordering = ["-capability_score"]

    def get_absolute_url(self) -> str:
        return reverse("coding-agent-detail", kwargs={"slug": self.slug})


class GitHubProject(TimestampedModel):
    repo = models.CharField(max_length=180, unique=True)
    category = models.CharField(max_length=80, db_index=True)
    description = models.TextField(blank=True)
    stars = models.PositiveIntegerField(default=0)
    stars_7d = models.IntegerField(default=0)
    forks = models.PositiveIntegerField(default=0)
    open_issues = models.PositiveIntegerField(default=0)
    pushed_at = models.DateTimeField(null=True, blank=True)
    momentum_score = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    homepage = models.URLField(max_length=800)
    source = models.ForeignKey(Source, on_delete=models.PROTECT, null=True, blank=True)
    data_as_of = models.DateTimeField(null=True, blank=True)
    quality_status = models.CharField(
        max_length=20,
        choices=Observation.Quality.choices,
        default=Observation.Quality.FRESH,
    )
    archived = models.BooleanField(default=False, db_index=True)
    is_fork = models.BooleanField(default=False)
    license_spdx = models.CharField(max_length=60, blank=True)

    class Meta:
        ordering = ["-momentum_score", "-stars"]


class GitHubProjectSnapshot(TimestampedModel):
    project = models.ForeignKey(GitHubProject, on_delete=models.CASCADE, related_name="snapshots")
    snapshot_date = models.DateField(db_index=True)
    stars = models.PositiveIntegerField(default=0)
    forks = models.PositiveIntegerField(default=0)
    open_issues = models.PositiveIntegerField(default=0)
    pushed_at = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField()
    batch_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    source = models.ForeignKey(Source, on_delete=models.PROTECT)

    class Meta:
        ordering = ["-snapshot_date", "project__repo"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "snapshot_date"], name="unique_github_project_daily_snapshot"
            )
        ]


class GlossaryTerm(TimestampedModel):
    slug = models.SlugField(max_length=120, unique=True)
    term = models.CharField(max_length=160)
    term_en = models.CharField(max_length=160, blank=True)
    category = models.CharField(max_length=80, db_index=True)
    subcategory = models.CharField(max_length=80, blank=True)
    difficulty = models.CharField(max_length=30, default="中级")
    definition = models.TextField()
    formula = models.TextField(blank=True)
    interpretation = models.TextField(blank=True)
    tags = models.JSONField(default=list)
    source_url = models.URLField(max_length=800, blank=True)

    class Meta:
        ordering = ["category", "term"]


class OptionContract(TimestampedModel):
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name="options")
    expiry = models.DateField()
    strike = models.DecimalField(max_digits=16, decimal_places=4)
    option_type = models.CharField(max_length=4, choices=[("call", "Call"), ("put", "Put")])
    open_interest = models.PositiveIntegerField(default=0)
    volume = models.PositiveIntegerField(default=0)
    implied_volatility = models.DecimalField(max_digits=8, decimal_places=5, null=True, blank=True)
    delta = models.DecimalField(max_digits=8, decimal_places=5, null=True, blank=True)
    gamma = models.DecimalField(max_digits=12, decimal_places=8, null=True, blank=True)
    as_of = models.DateTimeField()
    source = models.ForeignKey(Source, on_delete=models.PROTECT)
    quality_status = models.CharField(
        max_length=20, choices=Observation.Quality.choices, default=Observation.Quality.ESTIMATED
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["instrument", "expiry", "strike", "option_type"],
                name="unique_option_contract",
            )
        ]


class CFTCPosition(TimestampedModel):
    """Weekly Commitments of Traders position by contract and trader group."""

    report_type = models.CharField(max_length=30, default="tff-futures")
    report_date = models.DateField(db_index=True)
    published_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Initial PRE row publication timestamp from the Socrata :created_at field.",
    )
    source_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Latest PRE row revision timestamp from the Socrata :updated_at field.",
    )
    market_code = models.CharField(max_length=24, db_index=True)
    market_name = models.CharField(max_length=240)
    trader_group = models.CharField(max_length=40, db_index=True)
    long_positions = models.BigIntegerField()
    short_positions = models.BigIntegerField()
    open_interest = models.BigIntegerField(null=True, blank=True)
    fetched_at = models.DateTimeField()
    batch_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    source = models.ForeignKey(Source, on_delete=models.PROTECT)
    quality_status = models.CharField(
        max_length=20,
        choices=Observation.Quality.choices,
        default=Observation.Quality.FRESH,
    )

    @property
    def net_position(self) -> int:
        return self.long_positions - self.short_positions

    class Meta:
        ordering = ["-report_date", "market_name", "trader_group"]
        constraints = [
            models.UniqueConstraint(
                fields=["report_type", "report_date", "market_code", "trader_group"],
                name="unique_cftc_position_snapshot",
            )
        ]
        indexes = [models.Index(fields=["market_code", "trader_group", "-report_date"])]


class TreasuryAuction(TimestampedModel):
    cusip = models.CharField(max_length=16)
    security_type = models.CharField(max_length=40)
    security_term = models.CharField(max_length=40)
    announcement_date = models.DateField(null=True, blank=True)
    auction_date = models.DateField(db_index=True)
    issue_date = models.DateField(null=True, blank=True)
    maturity_date = models.DateField(null=True, blank=True)
    offering_amount = models.DecimalField(max_digits=24, decimal_places=2, null=True, blank=True)
    total_tendered = models.DecimalField(max_digits=24, decimal_places=2, null=True, blank=True)
    total_accepted = models.DecimalField(max_digits=24, decimal_places=2, null=True, blank=True)
    bid_to_cover_ratio = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    high_yield = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    indirect_bidder_accepted = models.DecimalField(
        max_digits=24, decimal_places=2, null=True, blank=True
    )
    direct_bidder_accepted = models.DecimalField(
        max_digits=24, decimal_places=2, null=True, blank=True
    )
    primary_dealer_accepted = models.DecimalField(
        max_digits=24, decimal_places=2, null=True, blank=True
    )
    fetched_at = models.DateTimeField()
    batch_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    source = models.ForeignKey(Source, on_delete=models.PROTECT)
    quality_status = models.CharField(
        max_length=20,
        choices=Observation.Quality.choices,
        default=Observation.Quality.FRESH,
    )

    class Meta:
        ordering = ["-auction_date", "security_type", "security_term"]
        constraints = [
            models.UniqueConstraint(
                fields=["cusip", "auction_date"], name="unique_treasury_auction"
            )
        ]
