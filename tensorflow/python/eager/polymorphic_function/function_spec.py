# Copyright 2022 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Defines an input type specification for tf.function."""

import functools
import inspect
from typing import Any, Dict, Tuple
import weakref

import numpy as np
import six

from tensorflow.core.function import trace_type
from tensorflow.core.function.polymorphism import function_type as function_type_lib
from tensorflow.python.eager.polymorphic_function import composite_tensor_utils
from tensorflow.python.framework import composite_tensor
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_spec
from tensorflow.python.framework import type_spec
from tensorflow.python.ops import resource_variable_ops
from tensorflow.python.util import _pywrap_utils
from tensorflow.python.util import nest
from tensorflow.python.util import tf_inspect

# Sentinel value used by with ConcreteFunction's structured signature to
# indicate that a non-tensor parameter should use the value that was
# specified when the concrete function was created.
BOUND_VALUE = object()


def to_fullargspec(function_type: function_type_lib.FunctionType,
                   default_values: Dict[str, Any],
                   is_bound_method: bool) -> inspect.FullArgSpec:
  """Generates backwards compatible FullArgSpec from FunctionType."""
  args = []
  varargs = None
  varkw = None
  defaults = []
  kwonlyargs = []
  kwonlydefaults = {}

  for parameter in function_type.parameters.values():
    if parameter.kind in [
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD
    ]:
      args.append(parameter.name)
      if parameter.default is not inspect.Parameter.empty:
        defaults.append(default_values[parameter.name])
    elif parameter.kind is inspect.Parameter.KEYWORD_ONLY:
      kwonlyargs.append(parameter.name)
      if parameter.default is not inspect.Parameter.empty:
        kwonlydefaults[parameter.name] = default_values[parameter.name]
    elif parameter.kind is inspect.Parameter.VAR_POSITIONAL:
      varargs = parameter.name
    elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
      varkw = parameter.name

  if (is_bound_method and (not args or args[0] != "self")):
    args.insert(0, "self")

  return inspect.FullArgSpec(
      args,
      varargs,
      varkw,
      tuple(defaults) if defaults else None,
      kwonlyargs,
      kwonlydefaults if kwonlydefaults else None,
      annotations={})


def _to_default_values(fullargspec):
  """Returns default values from the function's inspected fullargspec."""
  if fullargspec.defaults is not None:
    defaults = {
        name: value for name, value in zip(
            fullargspec.args[-len(fullargspec.defaults):], fullargspec.defaults)
    }
  else:
    defaults = {}

  if fullargspec.kwonlydefaults is not None:
    defaults.update(fullargspec.kwonlydefaults)

  defaults = {
      function_type_lib.sanitize_arg_name(name): value
      for name, value in defaults.items()
  }

  return defaults


def to_function_type(fullargspec):
  """Generates FunctionType and default values from fullargspec."""
  default_values = _to_default_values(fullargspec)
  parameters = []

  for arg in fullargspec.args:
    arg_name = function_type_lib.sanitize_arg_name(arg)
    parameters.append(
        function_type_lib.Parameter(
            arg_name, function_type_lib.Parameter.POSITIONAL_OR_KEYWORD,
            arg_name in default_values, None))

  if fullargspec.varargs is not None:
    parameters.append(
        function_type_lib.Parameter(fullargspec.varargs,
                                    function_type_lib.Parameter.VAR_POSITIONAL,
                                    False, None))

  for kwarg in fullargspec.kwonlyargs:
    parameters.append(
        function_type_lib.Parameter(
            function_type_lib.sanitize_arg_name(kwarg),
            function_type_lib.Parameter.KEYWORD_ONLY, kwarg in default_values,
            None))

  if fullargspec.varkw is not None:
    parameters.append(
        function_type_lib.Parameter(fullargspec.varkw,
                                    function_type_lib.Parameter.VAR_KEYWORD,
                                    False, None))

  return function_type_lib.FunctionType(parameters), default_values


def to_input_signature(function_type):
  """Extracts an input_signature from function_type instance."""
  constrained_parameters = list(function_type.parameters.keys())

  # self does not have a constraint in input_signature
  if "self" in constrained_parameters:
    constrained_parameters.pop(0)

  # There are no parameters to constrain.
  if not constrained_parameters:
    return tuple()

  constraints = []
  for parameter_name in constrained_parameters:
    parameter = function_type.parameters[parameter_name]
    constraint = None
    if parameter.type_constraint:
      # Generate legacy constraint representation.
      constraint = parameter.type_constraint.placeholder_value(
          trace_type.InternalPlaceholderContext(unnest_only=True)
      )
      if any(
          not isinstance(arg, tensor_spec.TensorSpec)
          for arg in nest.flatten([constraint], expand_composites=True)):
        # input_signature only supports TensorSpec composites.
        constraint = None

    if constraint is not None:
      constraints.append(constraint)
    else:
      # input_signatures are contiguous (can optionally skip default values).
      break

  # If the list is empty then there was no input_signature specified.
  return tuple(constraints) if constraints else None


# TODO(b/214462107): Clean up and migrate to core/function when unblocked.
class FunctionSpec(object):
  """Specification of how to bind arguments to a function."""

  @classmethod
  def from_function_and_signature(cls,
                                  python_function,
                                  input_signature,
                                  is_pure=False,
                                  jit_compile=None):
    """Creates a FunctionSpec instance given a python function and signature.

    Args:
      python_function: a function to inspect
      input_signature: a signature of the function (None, if variable)
      is_pure: if True all input arguments (including variables and constants)
        will be converted to tensors and no variable changes allowed.
      jit_compile: see `tf.function`

    Returns:
      instance of FunctionSpec
    """
    _validate_signature(input_signature)
    _validate_python_function(python_function, input_signature)

    function_type = function_type_lib.FunctionType.from_callable(
        python_function)
    default_values = function_type_lib.FunctionType.get_default_values(
        python_function)

    is_bound_method = inspect.ismethod(python_function)

    if input_signature is not None:
      input_signature = tuple(input_signature)
      function_type = function_type_lib.add_type_constraints(
          function_type, input_signature, default_values)

    # Get the function's name.  Remove functools.partial wrappers if necessary.
    while isinstance(python_function, functools.partial):
      python_function = python_function.func
    name = getattr(python_function, "__name__", "f")

    return FunctionSpec(
        function_type,
        default_values,
        is_bound_method,
        is_pure=is_pure,
        jit_compile=jit_compile,
        name=name)

  @classmethod
  def from_fullargspec_and_signature(cls,
                                     fullargspec,
                                     is_bound_method,
                                     input_signature,
                                     is_pure=False,
                                     name=None,
                                     jit_compile=None):
    """Construct FunctionSpec from legacy FullArgSpec format."""
    function_type, default_values = to_function_type(fullargspec)
    if input_signature:
      input_signature = tuple(input_signature)
      function_type = function_type_lib.add_type_constraints(
          function_type, input_signature, default_values)

    return FunctionSpec(function_type, default_values, is_bound_method, is_pure,
                        name, jit_compile)

  def __init__(self,
               function_type,
               default_values,
               is_bound_method,
               is_pure=False,
               name=None,
               jit_compile=None):
    """Constructs a FunctionSpec describing a python function.

    Args:
      function_type: A FunctionType describing the python function signature.
      default_values: Dictionary mapping parameter names to default values.
      is_bound_method: True if the underlying function is a bound method.
      is_pure: if True all input arguments (including variables and constants)
        will be converted to tensors and no variable changes allowed.
      name: Name of the function
      jit_compile: see `tf.function`.
    """
    self._function_type = function_type
    self._default_values = default_values
    self._fullargspec = to_fullargspec(function_type, default_values,
                                       is_bound_method)
    self._is_bound_method = is_bound_method
    self._is_pure = is_pure
    self._jit_compile = jit_compile

    # TODO(edloper): Include name when serializing for SavedModel?
    self._name = name or "f"

    if self._is_bound_method:
      # Remove `self`: default arguments shouldn't be matched to it.
      # TODO(b/127938157): Should this error out if there is no arg to
      # be removed?
      args = self.fullargspec.args[1:]
    else:
      args = self.fullargspec.args

    # A cache mapping from argument name to index, for canonicalizing
    # arguments that are called in a keyword-like fashion.
    self._args_to_indices = {arg: i for i, arg in enumerate(args)}
    self._arg_names = args

    # A cache mapping from arg index to default value, for canonicalization.
    default_values = self.fullargspec.defaults
    offset = len(args) - len(default_values or [])
    self._arg_indices_to_default_values = {
        offset + index: default
        for index, default in enumerate(default_values or [])
    }
    self._arg_indices_no_default_values = set(range(len(args))) - set(
        self._arg_indices_to_default_values)

    input_signature = to_input_signature(function_type)
    _validate_signature(input_signature)
    if input_signature is None:
      self._input_signature = None
    else:
      self._input_signature = tuple(input_signature)
      self._flat_input_signature = tuple(
          nest.flatten(input_signature, expand_composites=True))
    self.validate_input_signature_with_argspec()

  @property
  def default_values(self):
    """Returns dict mapping parameter names to default values."""
    return self._default_values

  @property
  def function_type(self):
    """Returns a FunctionType representing the Python function signature."""
    return self._function_type

  @property
  def fullargspec(self):
    return self._fullargspec

  # TODO(fmuham): Rename to is_bound_method.
  @property
  def is_method(self):
    """Returns True if the function is a method with a class instance bound."""
    return self._is_bound_method

  @property
  def args_to_indices(self):
    return self._args_to_indices

  @property
  def kwargs_to_include(self):
    return self._kwargs_to_include

  @property
  def input_signature(self):
    return self._input_signature

  @property
  def flat_input_signature(self):
    return self._flat_input_signature

  @property
  def is_pure(self):
    return self._is_pure

  @property
  def jit_compile(self):
    return self._jit_compile

  @property
  def arg_names(self):
    return self._arg_names

  @property
  def vararg_name(self):
    return self._fullargspec.varargs

  @property
  def varkw_name(self):
    return self._fullargspec.varkw

  def make_canonicalized_monomorphic_type(
      self,
      args: Any,
      kwargs: Any,
      captures: Any = None,
  ) -> Tuple[function_type_lib.FunctionType,
             trace_type.WeakrefDeletionObserver]:
    """Generates function type given the function arguments."""
    if captures is None:
      captures = dict()

    kwargs = {
        function_type_lib.sanitize_arg_name(name): value
        for name, value in kwargs.items()
    }

    _, function_type, type_context = (
        function_type_lib.canonicalize_to_monomorphic(
            args, kwargs, self.default_values, captures, self.function_type
        )
    )

    return function_type, type_context

  def signature_summary(self, default_values=False):
    """Returns a string summarizing this function's signature.

    Args:
      default_values: If true, then include default values in the signature.

    Returns:
      A `string`.
    """
    args = list(self._arg_names)
    if default_values:
      for (i, default) in self._arg_indices_to_default_values.items():
        args[i] += "={}".format(default)
    if self._fullargspec.kwonlyargs:
      args.append("*")
      for arg_name in self._fullargspec.kwonlyargs:
        args.append(arg_name)
        if default_values and arg_name in self._fullargspec.kwonlydefaults:
          args[-1] += "={}".format(self._fullargspec.kwonlydefaults[arg_name])
    return f"{self._name}({', '.join(args)})"

  def validate_input_signature_with_argspec(self):
    """Checks the python_function's args to be valid against input_signature."""
    if self.input_signature is not None:
      arglen = len(self.input_signature)
      arg_names_len = len(self.arg_names)
      defaults = self.fullargspec.defaults or ()
      unbound_self_arg = 1 if (not self.is_method and arg_names_len > 0 and
                               self.arg_names[0] == "self") else 0
      if not all(d is BOUND_VALUE for d in defaults):
        default_arg_len = len(defaults)
        required_arg_len = arg_names_len - default_arg_len - unbound_self_arg
        # The input signature must cover all required function arguments.
        if arglen < required_arg_len:
          missing_tensor_specs = self.arg_names[arglen:required_arg_len]
          raise TypeError(
              f"The decorated tf.function has {required_arg_len} "
              f"required argument(s), but tf.function was only passed an "
              f"input_signature of length {arglen}. This covers {arglen} "
              f"required argument(s): {self.arg_names[:arglen]}, "
              f"but TensorSpecs are still required for the remaining "
              f"{len(missing_tensor_specs)} argument(s):"
              f" {missing_tensor_specs}.")

  def validate_inputs_with_signature(self, args, kwargs):
    """Checks args and kwargs against the specified input_signature."""
    if kwargs:
      raise ValueError("Cannot define a TensorFlow function from a Python "
                       "function with keyword arguments when "
                       "input_signature is provided, got keyword arguments "
                       f"({kwargs}) with input_signature "
                       f"({self.input_signature}).")
    if args:
      # If args are provided, they must match the input signature.
      input_signature_args = args[:len(self.input_signature)]
      if not is_same_structure(self.input_signature, input_signature_args):
        raise ValueError("Structure of Python function inputs does not match "
                         f"input_signature: inputs ({args}), "
                         f"input_signature ({self.input_signature}).")
      flat_inputs = nest.flatten(input_signature_args, expand_composites=True)
      if any(not isinstance(arg, (ops.Tensor, tensor_spec.DenseSpec,
                                  resource_variable_ops.BaseResourceVariable))
             for arg in flat_inputs):
        raise ValueError("When input_signature is provided, all inputs to "
                         "the Python function must be Tensors, Variables, "
                         "tf.TensorSpec or tf.VariableSpec objects.")
      if any(not spec.is_compatible_with(other)
             for spec, other in zip(self.flat_input_signature, flat_inputs)):
        raise ValueError("Python inputs incompatible with input_signature: "
                         f"inputs ({args}), input_signature "
                         f"({self.input_signature}).")

  def canonicalize_function_inputs(self, args, kwargs):
    """Canonicalizes `args` and `kwargs`.

    Canonicalize the inputs to the Python function using a `FunctionSpec`
    instance. In particular, we parse the varargs and kwargs that the
    original function was called with into a tuple corresponding to the
    Python function's positional (named) arguments and a dictionary
    corresponding to its kwargs.  Missing default arguments are added.

    If this `FunctionSpec` has an input signature, then it is used to convert
    arguments to tensors; otherwise, any inputs containing numpy arrays are
    converted to tensors.

    Additionally, any inputs containing numpy arrays are converted to Tensors.

    Args:
      args: The varargs this object was called with.
      kwargs: The keyword args this function was called with.

    Returns:
      A canonicalized ordering of the inputs, as well as full and filtered
      (Tensors and Variables only) versions of their concatenated flattened
      representations, represented by a tuple in the form (args, kwargs,
      flat_args, filtered_flat_args). Here: `args` is a full list of bound
      arguments, and `kwargs` contains only true keyword arguments, as opposed
      to named arguments called in a keyword-like fashion.

    Raises:
      ValueError: If a keyword in `kwargs` cannot be matched with a positional
        argument when an input signature is specified, or when the inputs
        do not conform to the input signature.
    """
    if self.is_pure:
      args, kwargs = _convert_variables_to_tensors(args, kwargs)
    args, kwargs = self.bind_function_inputs(args, kwargs)
    args, kwargs = cast_inputs(args, kwargs, self.input_signature)
    filtered_flat_args = filter_function_inputs(args, kwargs)

    return args, kwargs, filtered_flat_args

  def bind_function_inputs(self, args, kwargs):
    """Bind `args` and `kwargs` into a canonicalized signature args, kwargs."""
    sanitized_kwargs = {
        function_type_lib.sanitize_arg_name(k): v for k, v in kwargs.items()
    }
    if len(kwargs) != len(sanitized_kwargs):
      raise ValueError(f"Name collision after sanitization. Please rename "
                       f"tf.function input parameters. Original: "
                       f"{sorted(kwargs.keys())}, Sanitized: "
                       f"{sorted(sanitized_kwargs.keys())}")

    try:
      bound_arguments = self.function_type.bind_with_defaults(
          args, sanitized_kwargs, self.default_values)
    except Exception as e:
      raise TypeError(
          f"Binding inputs to tf.function `{self._name}` failed due to `{e}`."
          f"Received args: {args} and kwargs: {sanitized_kwargs} for signature:"
          f" {self.function_type}."
      ) from e
    return bound_arguments.args, bound_arguments.kwargs


def _validate_signature(signature):
  """Checks the input_signature to be valid."""
  if signature is None:
    return

  if not isinstance(signature, (tuple, list)):
    raise TypeError("input_signature must be either a tuple or a list, got "
                    f"{type(signature)}.")

  # TODO(xjun): Allow VariableSpec once we figure out API for de-aliasing.
  variable_specs = _get_variable_specs(signature)
  if variable_specs:
    raise TypeError(
        f"input_signature doesn't support VariableSpec, got {variable_specs}")

  if any(not isinstance(arg, tensor_spec.TensorSpec)
         for arg in nest.flatten(signature, expand_composites=True)):
    bad_args = [
        arg for arg in nest.flatten(signature, expand_composites=True)
        if not isinstance(arg, tensor_spec.TensorSpec)
    ]
    raise TypeError("input_signature must be a possibly nested sequence of "
                    f"TensorSpec objects, got invalid args {bad_args} with "
                    f"types {list(six.moves.map(type, bad_args))}.")


def _validate_python_function(python_function, input_signature):
  """Checks the python_function to be valid against the input_signature."""
  if not callable(python_function):
    raise TypeError(f"{python_function} is not a callable object.")

  if input_signature is not None:
    fullargspec = tf_inspect.getfullargspec(python_function)
    if set(fullargspec.kwonlyargs) - set(fullargspec.kwonlydefaults or ()):
      nodefault_kwonlyargs = set(fullargspec.kwonlyargs)
      if fullargspec.kwonlydefaults is not None:
        nodefault_kwonlyargs -= set(fullargspec.kwonlydefaults)
      raise ValueError("Cannot build TF function from "
                       f"{python_function.__name__}: keyword-only arguments "
                       "must have default values when input_signature is "
                       "provided. Got keyword-only arguments without default "
                       f"values: {sorted(nodefault_kwonlyargs)}.")


def is_same_structure(structure1, structure2, check_values=False):
  """Check two structures for equality, optionally of types and of values."""
  try:
    nest.assert_same_structure(structure1, structure2, expand_composites=True)
  except (ValueError, TypeError):
    return False
  if check_values:
    flattened1 = nest.flatten(structure1, expand_composites=True)
    flattened2 = nest.flatten(structure2, expand_composites=True)
    # First check the types to avoid AttributeErrors.
    if any(type(f1) is not type(f2) for f1, f2 in zip(flattened1, flattened2)):
      return False
    return flattened1 == flattened2
  return True


def _to_tensor_or_tensor_spec(x):
  return (x if isinstance(x, (ops.Tensor, tensor_spec.TensorSpec)) else
          ops.convert_to_tensor(x))


def _deterministic_dict_values(dictionary):
  return tuple(dictionary[key] for key in sorted(dictionary))


def _convert_variables_to_tensors(args, kwargs):
  args = [_to_tensor_or_tensor_spec(x) for x in args]
  kwargs = {kw: _to_tensor_or_tensor_spec(x) for kw, x in kwargs.items()}
  return tuple(args), kwargs


def cast_inputs(args, kwargs, input_signature):
  """Casts args, kwargs to TF values based on an optional input_signature."""
  if input_signature is None:
    args = cast_numpy_inputs(args)
  else:
    args = cast_inputs_to_signature(args, input_signature)
  kwargs = cast_numpy_inputs(kwargs)

  return args, kwargs


def cast_numpy_inputs(inputs):
  """Converts numpy array inputs to tensors."""
  flat_inputs = composite_tensor_utils.flatten_with_variables(inputs)

  # Check for NumPy arrays in arguments and convert them to Tensors.
  # TODO(nareshmodi): Skip ndarray conversion to tensor altogether, perhaps
  # finding a way to store them directly in the cache key (currently not
  # possible since ndarrays are not hashable).
  need_packing = False
  filtered_flat_inputs = []
  for index, value in enumerate(flat_inputs):
    if isinstance(value,
                  (ops.Tensor, resource_variable_ops.BaseResourceVariable)):
      filtered_flat_inputs.append(value)
    elif hasattr(value, "__array__") and not (
        hasattr(value, "_should_act_as_resource_variable") or
        isinstance(value, (np.str_, type, composite_tensor.CompositeTensor))):
      # This case is equivalent to _is_ndarray(value) == True
      a = value.__array__()
      if not isinstance(a, np.ndarray):
        raise TypeError(f"The output of __array__ must be an np.ndarray, "
                        f"got {type(a)} from {value}.")
      flat_inputs[index] = constant_op.constant(a)
      filtered_flat_inputs.append(flat_inputs[index])
      need_packing = True
  if need_packing:
    return nest.pack_sequence_as(
        structure=inputs,
        flat_sequence=nest.flatten(flat_inputs, expand_composites=True),
        expand_composites=True)
  else:
    return inputs


def cast_inputs_to_signature(inputs, input_signature):
  """Converts inputs to pass into a function with an explicit signature."""

  flat_input_signature = tuple(
      nest.flatten(input_signature, expand_composites=True))

  def format_error_message(inputs, input_signature):
    return ("  inputs: (\n" + "    " + ",\n    ".join(str(i) for i in inputs) +
            ")\n" + "  input_signature: (\n" + "    " +
            ",\n    ".join(str(i) for i in input_signature) + ")")

  try:
    flatten_inputs = nest.flatten_up_to(
        input_signature,
        inputs[:len(input_signature)],
        expand_composites=True,
        check_types=False)  # lists are convert to tuples for `tf.data`.
  except ValueError:
    raise ValueError("Structure of Python function inputs does not match "
                     "input_signature:\n"
                     f"{format_error_message(inputs, input_signature)}.")

  need_packing = False
  for index, (value,
              spec) in enumerate(zip(flatten_inputs, flat_input_signature)):
    if (isinstance(spec, tensor_spec.TensorSpec) and
        not isinstance(value, tensor_spec.TensorSpec) and
        not _pywrap_utils.IsTensor(value)):
      try:
        flatten_inputs[index] = ops.convert_to_tensor(
            value, dtype_hint=spec.dtype)
        need_packing = True
      except ValueError:
        raise ValueError("When input_signature is provided, all inputs to "
                         "the Python function must be convertible to "
                         "tensors:\n"
                         f"{format_error_message(inputs, input_signature)}.")

  if any(not spec.is_compatible_with(other)
         for spec, other in zip(flat_input_signature, flatten_inputs)):
    raise ValueError("Python inputs incompatible with input_signature:\n"
                     f"{format_error_message(inputs, input_signature)}.")

  if need_packing:
    inputs = nest.pack_sequence_as(
        structure=input_signature,
        flat_sequence=flatten_inputs,
        expand_composites=True)

  return inputs


def filter_function_inputs(args, kwargs):
  """Filters and flattens args and kwargs."""
  flat_inputs = composite_tensor_utils.flatten_with_variables(
      args) + composite_tensor_utils.flatten_with_variables(kwargs)

  for inp in flat_inputs:
    # TODO(b/183107079): Allow these once they're handled properly.
    if isinstance(inp, weakref.ref):
      raise ValueError(f"weakref input {inp} not supported for tf.function.")

  filtered_flat_inputs = [
      t for t in flat_inputs
      if isinstance(t, (ops.Tensor, resource_variable_ops.BaseResourceVariable))
  ]

  return filtered_flat_inputs


def _get_variable_specs(args):
  """Returns `VariableSpecs` from `args`."""
  variable_specs = []
  for arg in nest.flatten(args):
    if not isinstance(arg, type_spec.TypeSpec):
      continue
    if isinstance(arg, resource_variable_ops.VariableSpec):
      variable_specs.append(arg)
    elif not isinstance(arg, tensor_spec.TensorSpec):
      # arg is a CompositeTensor spec.
      variable_specs.extend(_get_variable_specs(arg._component_specs))  # pylint: disable=protected-access
  return variable_specs
