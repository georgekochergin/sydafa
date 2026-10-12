"""Pydantic-модели конфигурации, схемы данных, генераторов, DML и DSL эволюции схемы."""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --- Конфигурация источников ---

class SourceConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: Literal["mssql", "oracle"]
    host: str
    port: int
    reader: str = "reader"


# --- Генераторы синтетических данных ---

class SequenceGenerator(BaseModel):
    kind: Literal["sequence"] = "sequence"


class FakerGenerator(BaseModel):
    kind: Literal["faker"] = "faker"
    provider: str = Field(default="word", description="Метод Faker: company, name, word, address, email")


class ChoiceGenerator(BaseModel):
    kind: Literal["choice"] = "choice"
    values: list[Any] = Field(min_length=1, description="Список допустимых значений: ['Retail', 'Corporate', 'VIP']")


class RandomDecimalGenerator(BaseModel):
    kind: Literal["random_decimal"] = "random_decimal"
    min: float = 0.0
    max: float = 100000.0


class RandomDateGenerator(BaseModel):
    kind: Literal["random_date"] = "random_date"


GeneratorSpec = Annotated[
    Union[
        SequenceGenerator,
        FakerGenerator,
        ChoiceGenerator,
        RandomDecimalGenerator,
        RandomDateGenerator,
    ],
    Field(discriminator="kind"),
]


# --- Спецификация колонок и таблиц ---

_IDENT_REGEX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_identifier(v: str) -> str:
    if not _IDENT_REGEX.match(v):
        raise ValueError(f"Идентификатор '{v}' содержит недопустимые символы. Разрешены только латиница, цифры и '_'.")
    return v


class ColumnSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    type: str
    generator: GeneratorSpec | None = None

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return validate_identifier(v)


class ForeignKey(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    columns: list[str]
    ref_schema: str | None = None
    ref_table: str
    ref_columns: list[str]


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


class CreateTableOp(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    op: Literal["create_table"] = "create_table"
    schema_: str = Field(alias="schema")
    name: str
    primary_key: list[str]
    columns: list[ColumnSpec]
    foreign_keys: list[ForeignKey] = Field(default_factory=list)

    @field_validator("schema_", "name")
    @classmethod
    def check_names(cls, v: str) -> str:
        return validate_identifier(v)


class DropTableOp(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    op: Literal["drop_table"] = "drop_table"
    schema_: str = Field(alias="schema")
    name: str

    @field_validator("schema_", "name")
    @classmethod
    def check_names(cls, v: str) -> str:
        return validate_identifier(v)


class RenameTableOp(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    op: Literal["rename_table"] = "rename_table"
    schema_: str = Field(alias="schema")
    from_name: str
    to_name: str

    @field_validator("schema_", "from_name", "to_name")
    @classmethod
    def check_names(cls, v: str) -> str:
        return validate_identifier(v)


class TransferTableOp(BaseModel):
    op: Literal["transfer_table"] = "transfer_table"
    table: str
    from_schema: str
    to_schema: str

    @field_validator("table", "from_schema", "to_schema")
    @classmethod
    def check_names(cls, v: str) -> str:
        return validate_identifier(v)


class AddColumnOp(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    op: Literal["add_column"] = "add_column"
    schema_: str = Field(alias="schema")
    table: str
    column: ColumnSpec

    @field_validator("schema_", "table")
    @classmethod
    def check_names(cls, v: str) -> str:
        return validate_identifier(v)


class DropColumnOp(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    op: Literal["drop_column"] = "drop_column"
    schema_: str = Field(alias="schema")
    table: str
    name: str

    @field_validator("schema_", "table", "name")
    @classmethod
    def check_names(cls, v: str) -> str:
        return validate_identifier(v)


class RenameColumnOp(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    op: Literal["rename_column"] = "rename_column"
    schema_: str = Field(alias="schema")
    table: str
    from_name: str
    to_name: str

    @field_validator("schema_", "table", "from_name", "to_name")
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


class AppendRequest(BaseModel):
    rows: int = Field(gt=0, le=100_000)
    new_key_ratio: float = Field(default=0.1, ge=0.0, le=1.0)


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
