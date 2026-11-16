def topological(graph):
    order=[];pending={k:set(v) for k,v in graph.items()}
    while pending:
        ready=sorted(k for k,v in pending.items() if not v)
        if not ready:raise ValueError('reference_cycle')
        for key in ready:order.append(key);pending.pop(key)
        for parents in pending.values():parents.difference_update(ready)
    return order


def scc_order(graph):
    """Return strongly connected components in parents-first order.

    ``graph`` maps a table to the set of tables it references.  Tarjan emits a
    component only after every component it points at has been emitted, which
    is exactly the order needed to write referenced rows before referrers.
    Ties are broken lexicographically so repeated planning is deterministic.
    """
    index={};low={};on_stack=set();stack=[];components=[];counter=0
    def visit(node):
        nonlocal counter
        index[node]=low[node]=counter;counter+=1
        stack.append(node);on_stack.add(node)
        for parent in sorted(graph.get(node,())):
            if parent not in index:
                visit(parent);low[node]=min(low[node],low[parent])
            elif parent in on_stack:
                low[node]=min(low[node],index[parent])
        if low[node]==index[node]:
            component=[]
            while True:
                member=stack.pop();on_stack.remove(member);component.append(member)
                if member==node:break
            components.append(sorted(component))
    for node in sorted(graph):
        if node not in index:visit(node)
    return components


def cyclic_component(component,graph):
    return len(component)>1 or any(node in graph.get(node,()) for node in component)
