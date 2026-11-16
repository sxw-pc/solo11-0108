def topological(graph):
    order=[];pending={k:set(v) for k,v in graph.items()}
    while pending:
        ready=sorted(k for k,v in pending.items() if not v)
        if not ready:raise ValueError('reference_cycle')
        for key in ready:order.append(key);pending.pop(key)
        for parents in pending.values():parents.difference_update(ready)
    return order
