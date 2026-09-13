import math
from typing import Optional
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN, ROUND_UP

def round_price(price: float, tick_size: float, side: str) -> float:
    """
    Round price to the nearest tick_size.
    If side is 'buy', round down (to not cross spread if not intended).
    If side is 'sell', round up.
    """
    if tick_size <= 0:
        return price
    
    # We use Decimal for precision
    p = Decimal(str(price))
    t = Decimal(str(tick_size))
    
    # number of steps
    steps = p / t
    
    if side.lower() == 'buy':
        rounded_steps = steps.to_integral_value(rounding=ROUND_DOWN)
    elif side.lower() == 'sell':
        rounded_steps = steps.to_integral_value(rounding=ROUND_UP)
    else:
        rounded_steps = steps.to_integral_value(rounding=ROUND_HALF_UP)
        
    return float(rounded_steps * t)

def round_quantity(quantity: float, min_quantity: float, step_size: float = 0.0) -> float:
    """
    Round quantity down to step_size. Ensure it's >= min_quantity.
    Return 0.0 if below min_quantity.
    """
    step = step_size if step_size > 0 else min_quantity
    if step <= 0:
        return quantity
        
    q = Decimal(str(quantity))
    s = Decimal(str(step))
    
    steps = q / s
    rounded_steps = steps.to_integral_value(rounding=ROUND_DOWN)
    
    res = float(rounded_steps * s)
    if res < min_quantity:
        return 0.0
    return res
