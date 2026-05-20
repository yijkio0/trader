from datetime import datetime
def session_index():
    bearish=[
        ("1:00","1:30"),
        ("4:30","5:00"),
        ("13:00","13:30"),
        ("19:00","19:30")
    ]
    bullish=[
        ("3:30","4:00"),
        ("4:00","4:30"),
        ("13:30","14:00")
    ]
    now=datetime.now().time()
    for start,end in bearish:
        if datetime.strptime(start,"%H:%M").time()<=now<=datetime.strptime(end,"%H:%M").time():
            return "bearish"
    for start,end in bullish:
        if datetime.strptime(start,"%H:%M").time()<=now<=datetime.strptime(end,"%H:%M").time():
            return "bullish"
    return "neutral"
print(session_index())