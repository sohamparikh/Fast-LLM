# TODO: Use packaging.version? (Safer but extra requirement)
import abc
import enum
import logging
import pathlib
import typing
import warnings

import yaml

from fast_llm.config import Config, Field, FieldHint, check_field, config_class
from fast_llm.engine.config_utils.data_type import DataType
from fast_llm.utils import Assert

if typing.TYPE_CHECKING:
    from fast_llm.engine.multi_stage.config import FastLLMModelConfig
    from fast_llm.engine.multi_stage.fast_llm_model import FastLLMModel

logger = logging.getLogger(__name__)

# TODO: Use packaging.version? (Safer but extra requirement)
CHECKPOINT_VERSION = "0.1"
KNOWN_CHECKPOINT_VERSIONS = ("0", "0.1")


def export_safetensors_metadata(metadata):
    """
    Safetensor only accepts string entries, so we convert to string explicitly.
    We use yaml rather than json because json requires explicit quotation marks on strings, which breaks things.
    (ex. "format": "pt" becomes '"pt"' which breaks huggingface models.)
    We avoid using safe_dump for scalars because it adds junk ("\n...\n") at the end of the string
    (decoding is unaffected.)
    """
    return {
        key: str(value) if isinstance(value, (str, int, float, bool)) else yaml.safe_dump(value)
        for key, value in metadata.items()
    }


def import_safetensors_metadata(metadata):
    return {key: yaml.safe_load(value) for key, value in metadata.items()}


class CheckpointFormat(abc.ABC):
    # A data structure to make information about a checkpoint format accessible during validation.
    name: typing.ClassVar[str]
    support_optimizer: typing.ClassVar[bool] = True
    support_saving: typing.ClassVar[bool] = True
    support_loading: typing.ClassVar[bool] = True
    enforce_architecture_match: typing.ClassVar[bool] = False

    @classmethod
    @abc.abstractmethod
    def get_handler_class(cls) -> type["CheckpointHandler"]:
        pass

    @classmethod
    def __fast_llm_serialize__(cls):
        return cls.name


class DistributedCheckpointFormat(CheckpointFormat):
    # TODO v0.2: Add `enforce_version_match`
    name: typing.ClassVar[str] = "distributed"
    enforce_architecture_match: typing.ClassVar[bool] = True

    @classmethod
    def get_handler_class(cls):
        from fast_llm.engine.checkpoint.distributed import DistributedCheckpointHandler

        return DistributedCheckpointHandler


class StateDictCheckpointFormat(CheckpointFormat):
    name: typing.ClassVar[str] = "state_dict"

    @classmethod
    def get_handler_class(cls):
        from fast_llm.engine.checkpoint.state_dict import TrivialCheckpointHandler

        return TrivialCheckpointHandler


class ModelConfigType(str, enum.Enum):
    none = "none"
    architecture = "architecture"
    model = "model"
    fast_llm = "fast_llm"

    @property
    def load_architecture(self):
        return self != ModelConfigType.none

    @property
    def load_base_model(self):
        return self in (ModelConfigType.model, ModelConfigType.fast_llm)

    @property
    def load_fast_llm(self):
        return self == ModelConfigType.fast_llm


@config_class()
class CheckpointPathConfigBase(Config):
    _abstract = True
    path: pathlib.Path | None = Field(
        default=None,
        desc="Location of the checkpoint.",
        hint=FieldHint.core,
    )


@config_class()
class CheckpointConfigBase(Config):
    _abstract = True
    # Note: the `format` may be a str when configuring from file or cli.
    #   The actual class should be set through `setup` in a parent config validation.
    format: type[CheckpointFormat] = Field(
        default=StateDictCheckpointFormat,
        desc="Format of the checkpoint.",
        hint=FieldHint.core,
    )

    def setup(self, model_config: "FastLLMModelConfig"):
        assert not self._validated
        self.format = model_config.get_checkpoint_format(self.format)

    @classmethod
    def _from_dict(
        cls,
        default: dict[str, typing.Any],
        strict: bool = True,
        flat: bool = False,
    ):
        # TODO v0.2: Remove.
        cls._handle_renamed_field(default, "imported_type", "model_type")
        if "model_type" in default:
            warnings.warn(
                "`CheckpointConfigBase.model_type` is deprecated."
                " Instead, use the model name directly as the checkpoint format."
            )
            if default.get("format", None) in ("huggingface", "external"):
                default["format"] = default.get("model_type")
                if default["format"] is None:
                    default["format"] = "auto"
            del default["model_type"]
        return super()._from_dict(default, strict, flat)


@config_class()
class CheckpointStateConfigBase(Config):
    _abstract = True
    model_weights: bool = Field(default=True, desc="Save/load the model weights.", hint=FieldHint.feature)
    optimizer_state: bool = Field(default=False, desc="Save/load the optimizer state.", hint=FieldHint.feature)

    @classmethod
    def _from_dict(
        cls,
        default: dict[str, typing.Any],
        strict: bool = True,
        flat: bool = False,
    ):
        cls._handle_renamed_field(default, "load_weights", "model_weights")
        cls._handle_renamed_field(default, "load_optimizer", "optimizer_state")
        return super()._from_dict(default, strict, flat)


@config_class()
class CheckpointSaveConfigBase(Config):
    _abstract = True
    parameters_per_file: int = Field(
        default=2**32,
        desc="Limit the number of parameters saved in each file.",
        hint=FieldHint.feature,
        valid=check_field(Assert.geq, 2**20),
    )
    data_type: DataType | None = Field(
        default=None,
        desc="Data type to save the checkpoint.",
        hint=FieldHint.feature,
    )


@config_class()
class CheckpointSaveMetadataConfig(CheckpointPathConfigBase, CheckpointConfigBase):
    _abstract = False


@config_class()
class CheckpointSaveConfig(CheckpointSaveMetadataConfig, CheckpointStateConfigBase, CheckpointSaveConfigBase):
    _abstract = False


@config_class()
class CheckpointLoadMetadataConfig(CheckpointPathConfigBase, CheckpointConfigBase):
    _abstract = False

    load_config: ModelConfigType = Field(
        default=ModelConfigType.architecture,
        desc="Configuration to save/load.",
        hint=FieldHint.core,
    )

    def _validate(self):
        super()._validate()
        if self.format.enforce_architecture_match:
            assert self.load_config.load_architecture

    @property
    def compare_log_fn(self):
        return ValueError if self.load_config.load_architecture else logger.warning


@config_class()
class CheckpointLoadConfig(CheckpointLoadMetadataConfig, CheckpointStateConfigBase):
    _abstract = False


class CheckpointHandler(abc.ABC):
    format: typing.ClassVar[type[CheckpointFormat]]

    def __init__(self, model: "FastLLMModel"):
        self._model = model

    # TODO: save_metadata?

    @classmethod
    @abc.abstractmethod
    def load_metadata(cls, config: CheckpointLoadMetadataConfig):
        pass

    @abc.abstractmethod
    def save(self, config: CheckpointSaveConfig, metadata: dict):
        pass

    @abc.abstractmethod
    def load(self, config: CheckpointLoadConfig, metadata: dict):
        pass
