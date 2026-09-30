def doPost(request, session):
    data = request["data"]
    if not isinstance(data, dict):
        data = system.util.jsonDecode(unicode(data))
    result = system.tag.writeBlocking([data["path"]], [data["value"]])[0]
    return {"json": {"quality": unicode(result), "good": result.isGood()}}
