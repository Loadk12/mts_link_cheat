from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime
@dataclass
class MeetingState:
    name: Optional[str] = None
    url: Optional[str] = None
    active: bool = False
    last_health_ok: Optional[bool] = None
    last_health_ts: Optional[datetime] = None
    screenshot_path: Optional[str] = None
    page: any = field(default=None, repr=False, compare=False)
    context: any = field(default=None, repr=False, compare=False)
    browser: any = field(default=None, repr=False, compare=False)
STATE = MeetingState()
