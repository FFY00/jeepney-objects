# SPDX-License-Identifier: MIT

from __future__ import annotations


__version__ = '0.0.2'

import functools
import itertools
import logging
import types as _types  # to not conflict with our types module
import typing
import warnings
import xml.etree.ElementTree as ET

from typing import Any, Callable, Dict, Generator, List, Optional, Sequence, Tuple, Type

import dbus_objects.signature


if typing.TYPE_CHECKING:  # pragma: no cover
    import dbus_objects.integration


class _DBusDescriptorBase():
    '''
    Base descriptor class that implements DBus interface objects

    Having a descriptor class allows us to save data on the method owner and
    allows us to easily implement things like properties.
    '''
    def __init__(
        self,
        interface: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        self._interface_orig = interface
        self._interface = self._interface_orig
        self._name = dbus_objects.signature.dbus_case(name) if name else None
        self._list_name: Optional[str] = None

    @property
    def interface(self) -> str:
        if not self._interface:
            raise ValueError("Interface hasn't been set yet")
        return self._interface

    @property
    def name(self) -> str:
        if not self._name:
            raise ValueError("Name hasn't been set yet")
        return self._name

    def register_interface(self, obj: Any) -> None:
        if not self._interface_orig:
            if obj.default_interface_root:
                self._interface = '.'.join([obj.default_interface_root, obj._dbus_name])
            else:
                raise DBusObjectException(f'Missing interface in DBus method: {self.name}')

    def __set_name__(self, obj_type: Any, name: str) -> None:
        if not issubclass(obj_type, DBusObject):
            raise DBusObjectException(
                f'The {self.__class__.__name__} decorator can only be used inside DBusObject'
            )
        self._owner = obj_type
        self._descriptor_name = name

        # get the method list for our type and initialize it if necessary
        assert self._list_name
        self._method_list = getattr(self._owner, self._list_name) or []
        setattr(self._owner, self._list_name, self._method_list)

        # add ourselves to the owner method list
        self._method_list.append((self._descriptor_name, self))

    def __get__(self, obj: Any, obj_type: Any = None) -> Any:
        raise NotImplementedError('This should be implemented in a subclass')


class _DBusMethodBase(_DBusDescriptorBase):
    '''
    Base descriptor class that implements DBus interface objects based on a method
    '''
    def __init__(
        self,
        func: Callable[..., Any],
        interface: Optional[str] = None,
        name: Optional[str] = None,
        return_names: Optional[Sequence[str]] = None,
        multiple_returns: bool = False,
    ) -> None:
        super().__init__(interface, name if name else func.__name__)
        self._func = func
        self._return_names = return_names or []
        self._multiple_returns = multiple_returns

        self._input_signature = dbus_objects.signature.DBusSignature.from_parameters(
            self._func,
        )
        self._output_signature = dbus_objects.signature.DBusSignature.from_return(
            self._func,
            self._return_names,
            self._multiple_returns,
        )

    def __get__(self, obj: Any, obj_type: Any = None) -> Any:
        if obj is None:
            return self._func
        # construct interface from obj
        self.register_interface(obj)
        return _types.MethodType(self._func, obj)


class _DBusMethod(_DBusMethodBase):
    '''
    Descriptor class that implements a DBus method
    '''
    def __init__(
        self,
        func: Callable[..., Any],
        interface: Optional[str] = None,
        name: Optional[str] = None,
        return_names: Optional[Sequence[str]] = None,
        multiple_returns: bool = False,
    ) -> None:
        super().__init__(func, interface, name, return_names, multiple_returns)
        self._list_name = '_dbus_methods'

    @property
    def signature(self) -> Tuple[str, str]:
        return str(self._input_signature), str(self._output_signature)

    @property
    def xml(self) -> ET.Element:
        if not self._input_signature or not self._output_signature:
            raise ValueError("Signature hasn't been set yet")

        xml = ET.Element('method', {'name': self.name})

        for direction, signature, names in (
            ('in', list(self._input_signature), self._input_signature.names or []),
            ('out', list(self._output_signature), self._output_signature.names or []),
        ):
            for name, sig in itertools.zip_longest(names, signature):
                data = {
                    'direction': direction,
                    'type': sig,
                }
                if name:
                    data['name'] = name
                ET.SubElement(xml, 'arg', data)

        # TODO: export documentation
        return xml


class _DBusProperty(_DBusMethodBase):
    '''
    Descriptor class that implements a DBus property

    Works like a simpler :meth:`property`
    '''
    def __init__(
        self,
        func: Callable[..., Any],
        interface: Optional[str] = None,
        name: Optional[str] = None,
        return_names: Optional[Sequence[str]] = None,
        multiple_returns: bool = False,
    ) -> None:
        super().__init__(func, interface, name, return_names, multiple_returns)
        self._list_name = '_dbus_properties'
        self._setter: Optional[Callable[[Any, Any], Any]] = None
        # TODO: Verify signature
        # TODO: Allow emiting a signal when the value changes

    @property
    def signature(self) -> str:
        return str(self._output_signature)

    @property
    def xml(self) -> ET.Element:
        if not self._input_signature or not self._output_signature:
            raise ValueError("Signature hasn't been set yet")

        xml = ET.Element('property', {
            'name': self.name,
            'type': self.signature,
            'access': 'read' if not self._setter else 'readwrite',
        })

        # TODO: Support write-only properties
        # TODO: Export documentation
        return xml

    def __get__(self, obj: Any, obj_type: Any = None) -> Any:
        if obj is None:
            return self._func(obj)
        return self._func(obj)

    def __set__(self, obj: Any, value: Any) -> None:
        if self._setter is None:
            raise AttributeError(f'{self._descriptor_name} has no setter')
        self._setter(obj, value)

    def setter(self, value: Callable[[Any, Any], Any]) -> _DBusProperty:
        '''
        Decorator that registers the method as the property setter

        Works just like the built-in :meth:`property`
        '''
        self._setter = value
        return self


class _DBusSignal(_DBusDescriptorBase):
    '''
    Descriptor class that implements a DBus signal
    '''
    def __init__(
        self,
        types: Tuple[Type[Any], ...],
        named_types: Dict[str, Type[Any]],
        interface: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        super().__init__(interface, name)
        self.__logger = logging.getLogger(self.__class__.__name__)
        self._list_name = '_dbus_signals'
        if types and named_types:
            # TODO: support mixed?
            raise ValueError(
                f'{self.__class__.__name__} receives either positional '
                'arguments or keyword arguments as the signal types, '
                'but not both.'
            )
        if named_types:
            self._signature = dbus_objects.signature.DBusSignature(
                [*named_types.values()],
                [*named_types.keys()],
            )
        else:
            self._signature = dbus_objects.signature.DBusSignature(types)

    @property
    def signature(self) -> str:
        return str(self._signature)

    @property
    def xml(self) -> ET.Element:
        xml = ET.Element('signal', {'name': self.name})

        for name, sig in itertools.zip_longest(
            self._signature.names or [],
            list(self._signature),
        ):
            data = {
                'type': sig,
            }
            if name:
                data['name'] = name
            ET.SubElement(xml, 'arg', data)

        return xml

    def emit_signal_callback(self, owner: Any) -> Callable[[Any], None]:
        def emit_signal(*args: Any) -> None:
            for callback in owner._emit_signal_callbacks:
                try:
                    # TODO: export python signature (__signature__)
                    callback(self, body=args)
                except Exception as e:
                    self.__logger.info(
                        'An exception ocurred when try to emit signal '
                        f'{self.name} {args}: {e}'
                    )
        return emit_signal

    def __set_name__(self, obj_type: Any, name: str) -> None:
        super().__set_name__(obj_type, name)
        if not self._name:
            self._name = dbus_objects.signature.dbus_case(name)

    def __get__(self, obj: Any, obj_type: Any = None) -> Any:
        self.register_interface(obj)  # construct interface from obj
        return self.emit_signal_callback(obj)


def dbus_method(
    interface: Optional[str] = None,
    name: Optional[str] = None,
    return_names: Optional[Sequence[str]] = None,
    multiple_returns: bool = False,
) -> Callable[[Callable[..., Any]], _DBusMethod]:
    '''
    This decorator exports a function as a DBus method

    The function must have type annotations, they will be used to resolve the
    method signature.

    The function name will be used as the DBus method name unless otherwise
    specified in the arguments.

    :param interface: DBus interface name
    :param name: DBus method name
    :param return_names: Names of the return arguments
    :param multiple_returns: Returns multiple parameters
    '''
    def decorator(func: Callable[..., Any]) -> _DBusMethod:
        return _DBusMethod(func, interface, name, return_names, multiple_returns)
    return decorator


def dbus_property(
    interface: Optional[str] = None,
    name: Optional[str] = None,
    return_names: Optional[Sequence[str]] = None,
    multiple_returns: bool = False,
) -> Callable[[Callable[..., Any]], _DBusProperty]:
    '''
    This decorator exports a method as a DBus property

    Works just like :meth:`dbus_method` and :meth:`property`

    :param interface: DBus interface name
    :param name: DBus method name
    :param return_names: Names of the return arguments
    :param multiple_returns: Returns multiple parameters
    '''
    def decorator(func: Callable[..., Any]) -> _DBusProperty:
        return _DBusProperty(func, interface, name, return_names, multiple_returns)
    return decorator


def dbus_signal(*types: Type[Any], **named_types: Type[Any]) -> _DBusSignal:
    '''
    This method returns a DBus signal
    '''
    return _DBusSignal(types=types, named_types=named_types)


def custom_dbus_signal(
    *types: Type[Any],
    interface: Optional[str] = None,
    name: Optional[str] = None,
) -> Callable[..., _DBusSignal]:
    '''
    This method returns a custom DBus signal constructor
    '''
    def constructor(*types: Type[Any], **named_types: Type[Any]) -> _DBusSignal:
        return _DBusSignal(
            types=types,
            named_types=named_types,
            interface=interface,
            name=name
        )
    return constructor


_DBusMethodTupleInternal = Tuple[str, _DBusMethod]  # method name, method descriptor
_DBusMethodTuple = Tuple[Callable[..., Any], _DBusMethod]  # method, method descriptor

_DBusPropertyTupleInternal = Tuple[str, _DBusProperty]  # property name, property descriptor
_DBusPropertyTuple = Tuple[
    Callable[[], Any],
    Callable[[Any], Any],
    _DBusProperty
]  # getter, setter, descriptor

_DBusSignalTupleInternal = Tuple[str, _DBusSignal]  # method name, signal descriptor
_DBusSignalTuple = Tuple[Callable[..., Any], _DBusSignal]  # method, signal descriptor


class DBusObject():
    '''
    This class represents a DBus object. It should be subclassed and to export
    DBus methods, you must define typed functions with the
    :meth:`dbus_objects.dbus_object` decorator.
    '''
    # type -> method name list
    _dbus_methods: Optional[List[_DBusMethodTupleInternal]] = None
    _dbus_properties: Optional[List[_DBusPropertyTupleInternal]] = None
    _dbus_signals: Optional[List[_DBusSignalTupleInternal]] = None

    def __init__(self, name: Optional[str] = None, default_interface_root: Optional[str] = None):
        '''
        The class name will be used as the DBus object name unless otherwise
        specified in the arguments.

        :param name: DBus object name
        '''
        self.is_dbus_object = True
        self._emit_signal_callbacks: List[Callable[[_DBusSignal, str, Any], None]] = []
        self._dbus_name = dbus_objects.signature.dbus_case(
            name if name else type(self).__name__
        )
        self.default_interface_root = default_interface_root

    @property
    def dbus_name(self) -> str:
        return self._dbus_name

    def get_dbus_methods(self) -> Generator[_DBusMethodTuple, _DBusMethodTuple, None]:
        '''
        Generator that provides the DBus methods
        '''
        if not self._dbus_methods:
            return
        for method_name, descriptor in self._dbus_methods:
            yield getattr(self, method_name), descriptor

    def get_dbus_properties(self) -> Generator[_DBusPropertyTuple, _DBusPropertyTuple, None]:
        '''
        Generator that provides the DBus properties
        '''
        if not self._dbus_properties:
            return
        for property_name, descriptor in self._dbus_properties:
            descriptor.register_interface(self)  # explicitely register the interface
            yield (
                lambda: getattr(self, property_name),
                lambda value: setattr(self, property_name, value),
                descriptor,
            )

    def get_dbus_signals(self) -> Generator[_DBusSignalTuple, _DBusSignalTuple, None]:
        '''
        Generator that provides the DBus signals
        '''
        if not self._dbus_signals:
            return
        for signal_name, descriptor in self._dbus_signals:
            yield getattr(self, signal_name), descriptor

    def register_server(self, server: dbus_objects.integration.DBusServerBase, path: str) -> None:
        if not self._dbus_signals:
            return
        if not server.emit_signal_callback:
            warnings.warn(DBusObjectWarning(
                f"Object '{self.dbus_name}' emits signals but the server "
                f"'{server.__class__.__name__}' does not support this. "
                'Signals will not be emitted.'
            ))
            return
        self._emit_signal_callbacks.append(
            functools.partial(server.emit_signal_callback, path=path),
        )


class DBusObjectException(Exception):
    pass


class DBusObjectWarning(Warning):
    pass
