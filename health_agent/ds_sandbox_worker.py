"""Trusted entry point launched inside the macOS sandbox with a one-shot JSON request."""

import ast
import json
import resource
import statistics
import sys
import traceback


OPERATIONS = {
    "len": len, "sum": sum, "min": min, "max": max, "sorted": sorted,
    "round": round, "abs": abs, "mean": statistics.mean, "median": statistics.median,
}
NODES = {
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign,
    ast.Name, ast.Load, ast.Store, ast.Dict, ast.List, ast.Tuple, ast.Constant,
    ast.Subscript, ast.Slice, ast.ListComp, ast.DictComp, ast.comprehension, ast.Compare,
    ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.BinOp, ast.Sub,
    ast.Div, ast.If, ast.IfExp, ast.Call, ast.Eq, ast.NotEq, ast.Is, ast.IsNot,
    ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
}


def checked_tree(code: str) -> ast.Module:
    tree = ast.parse(code, filename="<generated_analysis>")
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError("Define exactly one function: analysis(rows, plan).")
    function = tree.body[0]
    arguments = function.args
    if (function.name != "analysis" or function.decorator_list or function.returns
            or [arg.arg for arg in arguments.args] != ["rows", "plan"]
            or any(arg.annotation for arg in arguments.args)
            or arguments.posonlyargs or arguments.kwonlyargs or arguments.vararg
            or arguments.kwarg or arguments.defaults):
        raise ValueError("Use the exact signature def analysis(rows, plan).")
    nodes = list(ast.walk(tree))
    for node in nodes:
        if type(node) not in NODES or isinstance(node, ast.FunctionDef) and node is not function:
            raise ValueError(f"Disallowed Python construct: {type(node).__name__}.")
        if isinstance(node, ast.Name) and (node.id.startswith("_") or node.id == "analysis"):
            raise ValueError("Reserved names are unavailable within analysis.")
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name) or node.func.id not in OPERATIONS):
            raise ValueError("Calls must use the provided numeric helpers.")
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            raise ValueError("Input rows and dictionaries are read-only.")
        if isinstance(node, (ast.ListComp, ast.DictComp)):
            if len(node.generators) != 1 or not isinstance(node.generators[0].iter, ast.Name):
                raise ValueError("Use one named input iterable per comprehension.")
            if any(child is not node and isinstance(child, (ast.ListComp, ast.DictComp, ast.Call, ast.Slice))
                   for child in ast.walk(node)):
                raise ValueError("Use flat comprehensions without function calls or slicing inside them.")
    if len(nodes) > 500:
        raise ValueError("Reduce the analysis to a single short function.")
    return tree


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (2, 3))
    try:
        request = json.loads(sys.stdin.read(256 * 1024))
        namespace = {"__builtins__": OPERATIONS.copy()}
        exec(compile(checked_tree(request["code"]), "<generated_analysis>", "exec"), namespace)
        result = namespace["analysis"](request["rows"], request["plan"])
        if not isinstance(result, dict) or len(result) > 16 or any(
            not isinstance(key, str) or len(key) > 80 or type(value) not in {int, float, type(None)}
            for key, value in result.items()
        ):
            raise ValueError("Return a flat dictionary of numeric statistical fields.")
        output = json.dumps({"result": result}, allow_nan=False)
        if len(output) > 8192:
            raise ValueError("Return only the requested statistical result fields.")
    except Exception as error:
        generated_frames = [frame for frame in traceback.extract_tb(error.__traceback__)
                            if frame.filename == "<generated_analysis>"]
        if generated_frames:
            line = generated_frames[-1].lineno
            feedback = f"{type(error).__name__} in generated analysis at line {line}."
        else:
            feedback = "".join(traceback.format_exception_only(error))[-1600:]
        output = json.dumps({"error": feedback})
    print(output)


if __name__ == "__main__":
    main()
