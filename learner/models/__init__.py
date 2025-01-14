from core.registry import Registry
from models.tf_keras_model import TFKerasModel
from models.tf_v1_model import TFV1Model

model_registry = Registry('Model')

from models.q_model import *
from models.ac_model import *
from models.q_model_keras import *
from models.ac_model_keras import *
from models.custom_model import *
