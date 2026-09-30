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
    if op == "survey":
        provider = params["provider"]
        paths = [
            unicode(r["fullPath"])
            for r in system.tag.browse(
                "[%s]" % provider, {"tagType": "AtomicTag", "recursive": True}
            ).getResults()
            if "/_types_/" not in unicode(r["fullPath"])
            and not unicode(r["fullPath"]).startswith("[%s]_types_" % provider)
        ]
        counts = {}
        bad = []
        for i in range(0, len(paths), 1000):
            chunk = paths[i : i + 1000]
            for path, qv in zip(chunk, system.tag.readBlocking(chunk)):
                name = unicode(qv.quality.getName())
                counts[name] = counts.get(name, 0) + 1
                if not qv.quality.isGood() and len(bad) < 50:
                    bad.append({"path": path, "quality": unicode(qv.quality)})
        return {"json": {"tags": len(paths), "qualities": counts, "bad": bad}}
    return {"json": {"error": "unknown op"}}
