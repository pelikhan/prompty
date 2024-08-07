import os
import json
import inspect
import contextlib
from pathlib import Path
from numbers import Number
from datetime import datetime
from pydantic import BaseModel
from functools import wraps, partial
from typing import Any, Callable, Dict, Iterator, List


class Tracer:
    _tracers: Dict[str, Callable[[str], Iterator[Callable[[str, Any], None]]]] = {}

    @classmethod
    def add(
        cls, name: str, tracer: Callable[[str], Iterator[Callable[[str, Any], None]]]
    ) -> None:
        cls._tracers[name] = tracer

    @classmethod
    def clear(cls) -> None:
        cls._tracers = {}

    @classmethod
    @contextlib.contextmanager
    def start(cls, name: str) -> Iterator[Callable[[str, Any], None]]:
        with contextlib.ExitStack() as stack:
            traces = [
                stack.enter_context(tracer(name)) for tracer in cls._tracers.values()
            ]
            yield lambda key, value: [trace(key, value) for trace in traces]


def to_dict(obj: Any) -> Dict[str, Any]:
    # simple json types
    if isinstance(obj, str) or isinstance(obj, Number) or isinstance(obj, bool):
        return obj
    # datetime
    elif isinstance(obj, datetime):
        return obj.isoformat()
    # safe Prompty obj serialization
    elif type(obj).__name__ == "Prompty":
        return obj.to_safe_dict()
    # safe PromptyStream obj serialization
    elif type(obj).__name__ == "PromptyStream":
        return "PromptyStream"
    # pydantic models have their own json serialization
    elif isinstance(obj, BaseModel):
        return obj.model_dump()
    # recursive list and dict
    elif isinstance(obj, list):
        return [to_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: v if isinstance(v, str) else to_dict(v) for k, v in obj.items()}
    elif isinstance(obj, Path):
        return str(obj)
    # cast to string otherwise...
    else:
        return str(obj)


def _name(func: Callable, args):
    if hasattr(func, "__qualname__"):
        signature = f"{func.__module__}.{func.__qualname__}"
    else:
        signature = f"{func.__module__}.{func.__name__}"

    # core invoker gets special treatment
    core_invoker = signature == "prompty.core.Invoker.__call__"
    if core_invoker:
        name = type(args[0]).__name__
        signature = f"{args[0].__module__}.{args[0].__class__.__name__}.invoke"
    else:
        name = func.__name__

    return name, signature


def _inputs(func: Callable, args, kwargs) -> dict:
    ba = inspect.signature(func).bind(*args, **kwargs)
    ba.apply_defaults()

    inputs = {k: to_dict(v) for k, v in ba.arguments.items() if k != "self"}

    return inputs


def _results(result: Any) -> dict:
    return to_dict(result) if result is not None else "None"


def _trace_sync(func: Callable = None, *, description: str = None) -> Callable:
    description = description or ""

    @wraps(func)
    def wrapper(*args, **kwargs):
        name, signature = _name(func, args)
        with Tracer.start(name) as trace:
            trace("signature", signature)
            if description and description != "":
                trace("description", description)

            inputs = _inputs(func, args, kwargs)
            trace("inputs", inputs)

            result = func(*args, **kwargs)
            trace("result", _results(result))

            return result

    return wrapper


def _trace_async(func: Callable = None, *, description: str = None) -> Callable:
    description = description or ""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        name, signature = _name(func, args)
        with Tracer.start(name) as trace:
            trace("signature", signature)
            if description and description != "":
                trace("description", description)

            inputs = _inputs(func, args, kwargs)
            trace("inputs", inputs)

            result = await func(*args, **kwargs)
            trace("result", _results(result))

            return result

    return wrapper


def trace(func: Callable = None, *, description: str = None) -> Callable:
    if func is None:
        return partial(trace, description=description)

    wrapped_method = _trace_async if inspect.iscoroutinefunction(func) else _trace_sync

    return wrapped_method(func, description=description)


class PromptyTracer:
    def __init__(self, output_dir: str = None) -> None:
        if output_dir:
            self.output = Path(output_dir).resolve().absolute()
        else:
            self.output = Path(Path(os.getcwd()) / ".runs").resolve().absolute()

        if not self.output.exists():
            self.output.mkdir(parents=True, exist_ok=True)

        self.stack: List[Dict[str, Any]] = []

    @contextlib.contextmanager
    def tracer(self, name: str) -> Iterator[Callable[[str, Any], None]]:
        try:
            self.stack.append({"name": name})
            frame = self.stack[-1]

            def add(key: str, value: Any) -> None:
                if key not in frame:
                    frame[key] = value
                # multiple values creates list
                else:
                    if isinstance(frame[key], list):
                        frame[key].append(value)
                    else:
                        frame[key] = [frame[key], value]

            yield add
        finally:
            frame = self.stack.pop()
            # if stack is empty, dump the frame
            if len(self.stack) == 0:
                trace_file = (
                    self.output
                    / f"{frame['name']}.{datetime.now().strftime('%Y%m%d.%H%M%S')}.ptrace"
                )

                with open(trace_file, "w") as f:
                    json.dump(frame, f, indent=4)
            # otherwise, append the frame to the parent
            else:
                if "__frames" not in self.stack[-1]:
                    self.stack[-1]["__frames"] = []
                self.stack[-1]["__frames"].append(frame)


@contextlib.contextmanager
def console_tracer(name: str) -> Iterator[Callable[[str, Any], None]]:
    try:
        print(f"Starting {name}")
        yield lambda key, value: print(f"{key}:\n{json.dumps(value, indent=4)}")
    finally:
        print(f"Ending {name}")
