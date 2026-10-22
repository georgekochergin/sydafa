"""Pydantic-модели конфигурации, схемы данных, генераторов, DML и DSL эволюции схемы."""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Self, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# --- Конфигурация источников ---

class SourceConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = ""
    type: Literal["mssql", "oracle"]
    host: str = ""
    port: int = 1433
    external_port: int | None = None
    reader: str = "sydafa"


# --- Генераторы синтетических данных ---

DateDim = Literal["y", "m", "d"]
AnchorPoint = Literal["start_of_year", "today", "start_of_quarter", "start_of_month", "end_of_year"]


class BaseGenerator(BaseModel):
    null_ratio: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Доля NULL-значений от 0.0 до 1.0 (вероятность генерации NULL)"
    )


class SequenceGenerator(BaseGenerator):
    kind: Literal["sequence"] = "sequence"


class UuidGenerator(BaseGenerator):
    kind: Literal["uuid"] = "uuid"


class FakerGenerator(BaseGenerator):
    kind: Literal["faker"] = "faker"
    provider: str = Field(default="word", description="Метод Faker: company, name, word, address, email")
    locale: str = Field(default="ru_RU", description="Локаль Faker, по умолчанию ru_RU")


class ChoiceGenerator(BaseGenerator):
    kind: Literal["choice"] = "choice"
    values: list[Any] = Field(min_length=1, description="Список допустимых значений: ['Retail', 'Corporate', 'VIP']")
    weights: list[float] | None = Field(default=None, description="Относительные веса выбора для значений values")

    @model_validator(mode="after")
    def validate_weights(self) -> Self:
        if self.weights is not None:
            if len(self.weights) != len(self.values):
                raise ValueError(
                    f"Количество весов weights ({len(self.weights)}) должно совпадать с количеством значений values ({len(self.values)})"
                )
            if any(w < 0 for w in self.weights):
                raise ValueError("Веса weights не могут быть отрицательными")
            if sum(self.weights) == 0:
                raise ValueError("Сумма весов weights должна быть больше 0")
        return self


class RandomDecimalGenerator(BaseGenerator):
    kind: Literal["random_decimal"] = "random_decimal"
    min: float = 0.0
    max: float = 100000.0


class RandomDateGenerator(BaseGenerator):
    kind: Literal["random_date"] = "random_date"
    anchor: AnchorPoint = Field(
        default="start_of_year",
        description="Опорная точка: start_of_year, today, start_of_quarter, start_of_month, end_of_year",
    )
    dim: DateDim = Field(default="d", description="Размерность смещения: 'y' (годы), 'm' (месяцы), 'd' (дни)")
    min_offset: int | None = Field(default=None, description="Минимальное смещение от anchor в единицах dim")
    max_offset: int | None = Field(default=None, description="Максимальное смещение от anchor в единицах dim")


class DateOffsetGenerator(BaseGenerator):
    kind: Literal["date_offset"] = "date_offset"
    ref_column: str = Field(description="Имя базовой колонки с датой в той же строке")
    dim: DateDim = Field(default="y", description="Размерность смещения: 'y' (годы), 'm' (месяцы), 'd' (дни)")
    min_offset: int = Field(default=1, description="Минимальное смещение (по умолчанию 1, строго > 0)")
    max_offset: int = Field(default=5, description="Максимальное смещение")


GeneratorSpec = Annotated[
    Union[
        SequenceGenerator,
        UuidGenerator,
        FakerGenerator,
        ChoiceGenerator,
        RandomDecimalGenerator,
        RandomDateGenerator,
        DateOffsetGenerator,
    ],
    Field(discriminator="kind"),
]


# --- Спецификация колонок, инвариантов и правил генерации ---

BATCH_ID_COLUMN = "_batch_id"
EXEC_BATCH_ID_COLUMN = "_exec_batch_id"

_IDENT_REGEX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENTITY_FQN_REGEX = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)$")


def validate_identifier(v: str) -> str:
    if not _IDENT_REGEX.match(v):
        raise ValueError(f"Идентификатор '{v}' содержит недопустимые символы. Разрешены только латиница, цифры и '_'.")
    return v


def validate_entity_fqn(entity_fqn: str) -> tuple[str, str]:
    """Проверяет формат schema.table и возвращает (schema, table)."""
    match = _ENTITY_FQN_REGEX.match(entity_fqn.strip())
    if not match:
        raise ValueError(
            f"Некорректный идентификатор сущности '{entity_fqn}'. Ожидается формат 'schema.table', "
            "где имена схемы и таблицы содержат только латиницу, цифры и символ '_'"
        )
    return match.group(1), match.group(2)


class ColumnSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    type: str | None = None
    generator: GeneratorSpec | None = None

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return validate_identifier(v)


# --- Инварианты связности данных и профили правил таблицы ---

class CumulativeBalanceInvariant(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["cumulative_balance"] = "cumulative_balance"
    amount_column: str = Field(description="Колонка суммы транзакции (приращения/дельты)")
    partition_by: list[str] = Field(
        default_factory=list,
        description="Опциональные колонки разреза счета (например, ['account_id']). Если не указаны, баланс рассчитывается глобально для всей таблицы.",
    )
    min_balance: float = Field(default=0.0, description="Минимальный допустимый баланс")
    initial_balance: float = Field(default=0.0, description="Начальный остаток для нового счета при отсутствии в БД")
    deposit_prob: float = Field(default=0.5, ge=0.0, le=1.0, description="Вероятность пополнения")
    deposit_range: tuple[float, float] = Field(default=(10.0, 1000.0), description="Диапазон сумм пополнения [min, max]")
    withdrawal_range: tuple[float, float] = Field(default=(10.0, 1000.0), description="Диапазон сумм списания [min, max]")
    validate_existing: bool = Field(
        default=False,
        description="False: мягкий режим (мутации свободны, append балансирует); True: строгий режим с валидацией мутаций.",
    )

    @field_validator("partition_by")
    @classmethod
    def check_partition_cols(cls, v: list[str]) -> list[str]:
        return [validate_identifier(col) for col in v]

    @field_validator("amount_column")
    @classmethod
    def check_amount_col(cls, v: str) -> str:
        return validate_identifier(v)

    @field_validator("deposit_range", "withdrawal_range")
    @classmethod
    def check_ranges(cls, v: tuple[float, float]) -> tuple[float, float]:
        min_v, max_v = v
        if min_v <= 0 or max_v <= 0:
            raise ValueError("Суммы диапазона должны быть строго больше 0")
        if min_v > max_v:
            raise ValueError(f"Минимум ({min_v}) не может превышать максимум ({max_v})")
        return (float(min_v), float(max_v))


TableInvariant = CumulativeBalanceInvariant


class TableRuleProfile(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    columns: dict[str, GeneratorSpec] = Field(default_factory=dict, description="Маппинг колонка -> генератор")
    invariants: list[TableInvariant] = Field(default_factory=list, description="Список инвариантов связности таблицы")


class ForeignKey(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    columns: list[str]
    ref_entity_fqn: str = Field(description="FQN целевой сущности в формате 'schema.table'")
    ref_columns: list[str]

    @field_validator("ref_entity_fqn")
    @classmethod
    def check_ref_entity_fqn(cls, v: str) -> str:
        validate_entity_fqn(v)
        return v.strip()

    @property
    def ref_schema(self) -> str:
        return self.ref_entity_fqn.split(".")[0].strip()

    @property
    def ref_table(self) -> str:
        return self.ref_entity_fqn.split(".")[1].strip()


class TableSchema(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    schema_: str = Field(alias="schema")
    primary_key: list[str]
    columns: list[ColumnSpec]
    foreign_keys: list[ForeignKey] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def check_table_name(cls, v: str) -> str:
        return validate_identifier(v)

    @field_validator("schema_")
    @classmethod
    def check_schema_name(cls, v: str) -> str:
        return validate_identifier(v)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


class LiveSchema(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schemas: list[str] = Field(default_factory=list)
    tables: dict[str, TableSchema] = Field(default_factory=dict)  # ключ: f"{schema}.{table}"


# --- Операции DSL эволюции схемы (POST /sources/{source}/schema/evolve) ---

class CreateSchemaOp(BaseModel):
    op: Literal["create_schema"] = "create_schema"
    name: str

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return validate_identifier(v)


class DropSchemaOp(BaseModel):
    op: Literal["drop_schema"] = "drop_schema"
    name: str

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return validate_identifier(v)


class RenameSchemaOp(BaseModel):
    op: Literal["rename_schema"] = "rename_schema"
    from_name: str
    to_name: str

    @field_validator("from_name", "to_name")
    @classmethod
    def check_names(cls, v: str) -> str:
        return validate_identifier(v)


class EntityFqnOpMixin(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    entity_fqn: str = Field(description="FQN сущности в формате 'schema.table'")

    @field_validator("entity_fqn")
    @classmethod
    def check_entity_fqn(cls, v: str) -> str:
        validate_entity_fqn(v)
        return v.strip()

    @property
    def schema_(self) -> str:
        return self.entity_fqn.split(".")[0].strip()

    @property
    def table(self) -> str:
        return self.entity_fqn.split(".")[1].strip()


class CreateTableOp(EntityFqnOpMixin):
    op: Literal["create_table"] = "create_table"
    primary_key: list[str]
    columns: list[ColumnSpec]
    foreign_keys: list[ForeignKey] = Field(default_factory=list)

    @property
    def name(self) -> str:
        return self.table


class DropTableOp(EntityFqnOpMixin):
    op: Literal["drop_table"] = "drop_table"

    @property
    def name(self) -> str:
        return self.table


class RenameTableOp(EntityFqnOpMixin):
    op: Literal["rename_table"] = "rename_table"
    to_name: str

    @property
    def from_name(self) -> str:
        return self.table

    @field_validator("to_name")
    @classmethod
    def check_to_name(cls, v: str) -> str:
        return validate_identifier(v)


class TransferTableOp(EntityFqnOpMixin):
    op: Literal["transfer_table"] = "transfer_table"
    to_schema: str

    @property
    def from_schema(self) -> str:
        return self.schema_

    @field_validator("to_schema")
    @classmethod
    def check_to_schema(cls, v: str) -> str:
        return validate_identifier(v)


class AddColumnOp(EntityFqnOpMixin):
    op: Literal["add_column"] = "add_column"
    column: ColumnSpec


class DropColumnOp(EntityFqnOpMixin):
    op: Literal["drop_column"] = "drop_column"
    name: str

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return validate_identifier(v)


class RenameColumnOp(EntityFqnOpMixin):
    op: Literal["rename_column"] = "rename_column"
    from_name: str
    to_name: str

    @field_validator("from_name", "to_name")
    @classmethod
    def check_names(cls, v: str) -> str:
        return validate_identifier(v)


EvolveOperation = Annotated[
    Union[
        CreateSchemaOp,
        DropSchemaOp,
        RenameSchemaOp,
        CreateTableOp,
        DropTableOp,
        RenameTableOp,
        TransferTableOp,
        AddColumnOp,
        DropColumnOp,
        RenameColumnOp,
    ],
    Field(discriminator="op"),
]


class EvolveRequest(BaseModel):
    operations: list[EvolveOperation]


class EvolveResult(BaseModel):
    status: str = "ok"
    executed: int
    applied: list[str]


# --- DML модели ---

class Condition(BaseModel):
    column: str
    operator: Literal["=", "!=", ">", "<", ">=", "<="]
    value: Any


class TruncateOp(EntityFqnOpMixin):
    op: Literal["truncate"] = "truncate"
    cascade: bool = False


class AppendOp(EntityFqnOpMixin):
    op: Literal["append"] = "append"
    rows: int = Field(default=1, gt=0, le=100_000)


class UpdateOp(EntityFqnOpMixin):
    op: Literal["update"] = "update"
    predicate: list[Condition] = Field(default_factory=list)
    set: dict[str, Any]
    limit: int | None = Field(default=None, gt=0, description="Обновить не более N случайных строк, удовлетворяющих предикату")


class DeleteOp(EntityFqnOpMixin):
    op: Literal["delete"] = "delete"
    predicate: list[Condition] = Field(default_factory=list)
    cascade: bool = False
    limit: int | None = Field(default=None, gt=0, description="Удалить не более N случайных строк, удовлетворяющих предикату")


MigrateOperation = Annotated[
    Union[
        CreateSchemaOp,
        DropSchemaOp,
        RenameSchemaOp,
        CreateTableOp,
        DropTableOp,
        RenameTableOp,
        TransferTableOp,
        AddColumnOp,
        DropColumnOp,
        RenameColumnOp,
        TruncateOp,
        AppendOp,
        UpdateOp,
        DeleteOp,
    ],
    Field(discriminator="op"),
]


class MigrateRequest(BaseModel):
    operations: list[MigrateOperation]


class MigrateResult(BaseModel):
    status: str = "ok"
    executed: int
    applied: list[dict[str, Any]]


# Алиасы операций исполнения
ExecOperation = MigrateOperation
ExecRequest = MigrateRequest
ExecResult = MigrateResult


# --- Декларативные модели sources.json ---

class ColumnDeclSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: str | None = None
    generator: GeneratorSpec | None = None


class TableDeclSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    primary_key: list[str] = Field(default_factory=list)
    columns: dict[str, ColumnDeclSpec] = Field(
        default_factory=dict,
        description="Маппинг имя колонки -> ColumnDeclSpec (словарь спецификации, для FK пустой словарь {})",
    )
    foreign_keys: list[ForeignKey] = Field(default_factory=list)


class SchemaDeclSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tables: dict[str, TableDeclSpec] = Field(
        default_factory=dict,
        description="Маппинг 'schema.table' -> TableDeclSpec",
    )


class SourceManifestSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: Literal["mssql", "oracle"]
    external_port: int | None = None
    schema_: SchemaDeclSpec = Field(default_factory=SchemaDeclSpec, alias="schema")
    rules: dict[str, TableRuleProfile] = Field(
        default_factory=dict,
        description="Маппинг 'schema.table' -> TableRuleProfile",
    )


class AppendRequest(BaseModel):
    rows: int = Field(gt=0, le=100_000)


class UpdateRequest(BaseModel):
    predicate: list[Condition]
    set: dict[str, Any]


class DeleteRequest(BaseModel):
    predicate: list[Condition]
    cascade: bool = False


# --- Интроспекция API ---

class TableRef(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_: str = Field(alias="schema")
    name: str


class SourceInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source: str
    type: str
    database: str
    host: str
    port: int
    reader: str
    schemas: list[str]
    tables: list[TableRef]


class ConnectionDetails(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: Literal["mssql", "oracle"]
    host: str
    port: int
    database: str
    user: str
    password: str
    jdbc_url: str


class SourceLifecycleStatus(BaseModel):
    source: str
    state: str
    last_used: str | None = None
    activated_until: str | None = Field(
        default=None,
        description="Время окончания активности контейнера в формате ISO 8601 UTC (например, 2026-10-14T12:15:00Z)",
    )
    ttl_minutes: int
    connection: ConnectionDetails | None = None
    host_port: int | None = None
    published: bool = False


class SourceReinitResult(BaseModel):
    status: str = "reinitialized"
    source: str
    applied_at: str
    operations_executed: int
    message: str
