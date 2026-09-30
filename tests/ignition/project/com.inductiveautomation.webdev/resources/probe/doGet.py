def doGet(request, session):
    params = request["params"]
    op = params.get("op")
    if op == "read":
        qv = system.tag.readBlocking([params["path"]])[0]
        value = qv.value
        if value is not None and not isinstance(value, (bool, basestring)):
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = unicode(value)
        return {
            "json": {"value": value, "quality": unicode(qv.quality), "good": qv.quality.isGood()}
        }
    if op == "alarms":
        events = system.alarm.queryStatus(source=[params["source"]])
        return {
            "json": [
                {
                    "source": unicode(e.getSource()),
                    "name": e.getName(),
                    "state": unicode(e.getState()),
                }
                for e in events
            ]
        }
    return {"json": {"error": "unknown op"}}
