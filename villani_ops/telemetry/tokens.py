def unavailable_warning(input_tokens:int, output_tokens:int)->str|None:
    return "Token counts were unavailable from runner output." if input_tokens == 0 and output_tokens == 0 else None
