import math, csv, json, random, time, hashlib, os
from pathlib import Path
from collections import Counter
import requests

DATA=Path('data'); OUT=Path('results')
URL=os.environ.get('OSRM_URL','http://localhost:5000').rstrip('/')

def write_csv(path, rows, fields=None):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        if fields is None: fields=list(dict.fromkeys(k for r in rows for k in r))
        if fields:
            w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)

def read_csv(path):
    with open(path,encoding='utf-8-sig',newline='') as f: return list(csv.DictReader(f))

def api(path, **params):
    r=requests.get(URL+path,params=params,timeout=60); r.raise_for_status(); j=r.json()
    if j.get('code')!='Ok': raise ValueError(j)
    return j

def nearest(lon,lat,limit):
    w=api(f'/nearest/v1/driving/{lon},{lat}',number=1)['waypoints'][0]
    if w['distance']>limit: return None
    return (*w['location'],w['distance'])

def coords_string(points): return ';'.join(f'{x:.7f},{y:.7f}' for x,y in points)

def matrix(points, mode='osrm', traffic=1.0):
    if mode=='synthetic':
        # Only for offline tests; never interpreted as road-network measurements.
        def km(a,b):
            x=(a[0]-b[0])*111*math.cos(math.radians((a[1]+b[1])/2)); y=(a[1]-b[1])*111
            return math.hypot(x,y)*1.25
        d=[[round(km(a,b)*1000) for b in points] for a in points]
        t=[[math.ceil(x/1000/55*3600) for x in row] for row in d]
    else:
        manifest=DATA/'osm_manifest.json'
        dataset=json.loads(manifest.read_text())['sha256'] if manifest.exists() else 'unversioned'
        key=hashlib.sha256((URL+'|'+dataset+'|'+coords_string(points)).encode()).hexdigest()
        cache=DATA/'matrix_cache'/f'{key}.json'; cache.parent.mkdir(parents=True,exist_ok=True)
        if cache.exists(): j=json.loads(cache.read_text())
        else:
            j=api('/table/v1/driving/'+coords_string(points),annotations='distance,duration')
            if any(x is None for row in j['durations']+j['distances'] for x in row):
                raise ValueError('Unreachable pair: inspect pool/OSRM graph; do not substitute straight line.')
            cache.write_text(json.dumps(j))
        d=[[round(x) for x in row] for row in j['distances']]
        t=j['durations']
    return d, [[math.ceil(x*traffic) for x in row] for row in t]

def make_requests(pool,cfg,scenario,seed,jitter=0):
    rng=random.Random(seed); rows=[]
    by={c:[p for p in pool if p['city']==c and p.get('kind')!='depot'] for c in ('TB','HN')}
    for h in range(cfg['first_hour'],cfg['last_hour']+1):
        for a,b in [('TB','HN'),('HN','TB')]:
            direction=a+'-'+b; n=max(0,cfg['profiles'][scenario][direction][h-cfg['first_hour']]+rng.randint(-jitter,jitter))
            pp=rng.sample(by[a],n); dd=rng.sample(by[b],n)
            for i,(p,d) in enumerate(zip(pp,dd)):
                start=h*3600; target=start+rng.randint(0, min(25,cfg['pickup_window_min']))*60
                # pickup window = preferred start to preferred start + tolerance (bounded by trip slot).
                latest=min(start+cfg['pickup_window_min']*60,target+cfg['pickup_tolerance_min']*60)
                r={'request_id':f'{scenario}-{seed}-{h:02d}-{direction}-{i+1:02d}', 'scenario':scenario,'seed':seed,'direction':direction,'slot_sec':start,'booked_sec':start-rng.randint(30,240)*60,'seats':1,'pickup_earliest_sec':target,'pickup_latest_sec':latest,'dropoff_latest_sec':start+cfg['trip_limit_min']*60}
                for prefix,pt in [('p',p),('d',d)]:
                    for k in ['osm_id','name','address','address_source','zone','lon','lat','route_lon','route_lat','snap_m']:
                        r[f'{prefix}_{k}']=pt.get(k,'')
                rows.append(r)
    return rows

def stops_csv(rows):
    result=[]
    for r in rows:
        for p,kind in [('p','pickup'),('d','dropoff')]:
            result.append({'request_id':r['request_id'],'direction':r['direction'],'slot_sec':r['slot_sec'],'type':kind,'seats_delta':int(r['seats'])*(1 if p=='p' else -1),'earliest_sec':r['pickup_earliest_sec'] if p=='p' else r['slot_sec'],'latest_sec':r['pickup_latest_sec'] if p=='p' else r['dropoff_latest_sec'],**{k:r[f'{p}_{k}'] for k in ['osm_id','name','address','address_source','zone','lon','lat','route_lon','route_lat','snap_m']}})
    return result

def build_problem(rows,a,b,depots,cfg,mode,traffic,slot):
    points=[depots[a]]; nodes=[{'kind':'start','service':0,'q':0,'lo':0,'hi':0}]; pairs=[]
    H=cfg['trip_limit_min']*60
    for r in rows:
        p=len(nodes)
        for prefix,kind,q,lo,hi,service in [('p','pickup',int(r['seats']),int(r['pickup_earliest_sec'])-slot,int(r['pickup_latest_sec'])-slot,cfg['pickup_service_sec']),('d','dropoff',-int(r['seats']),0,min(H,int(r['dropoff_latest_sec'])-slot),cfg['dropoff_service_sec'])]:
            points.append((float(r[f'{prefix}_route_lon']),float(r[f'{prefix}_route_lat'])))
            nodes.append({'kind':kind,'q':q,'lo':lo,'hi':hi,'service':service,'request_id':r['request_id']})
        pairs.append((p,p+1))
    points.append(depots[b]); nodes.append({'kind':'end','service':0,'q':0,'lo':0,'hi':H})
    distances,travel=matrix(points,mode,traffic)
    return dict(points=points,nodes=nodes,pairs=pairs,d=distances,t=travel,H=H,capacity=cfg['capacity'],detour=cfg['max_detour_min']*60)

def evaluate(P, route, times=None, allow_empty_overrun=False):
    nodes=P['nodes']; d=P['d']; t=P['t']; end=len(nodes)-1
    if route[0]!=0 or route[-1]!=end or len(set(route))!=len(route): return None
    if times is None:
        times=[0]
        for i,j in zip(route,route[1:]): times.append(max(nodes[j]['lo'],times[-1]+nodes[i]['service']+t[i][j]))
    if len(times)!=len(route) or times[0]!=0: return None
    seen=dict(zip(route,times)); load=0; dropped=False
    for k,(j,tm) in enumerate(zip(route,times)):
        n=nodes[j]; load+=n['q']
        if (tm<n['lo'] or tm>n['hi']) and not (allow_empty_overrun and len(route)==2 and j==end): return None
        if k and tm<times[k-1]+nodes[route[k-1]]['service']+t[route[k-1]][j]: return None
        if not 0<=load<=P['capacity']: return None
        if n['kind']=='dropoff': dropped=True
        if n['kind']=='pickup' and dropped: return None
    if load: return None
    metrics=[]
    for p,q in P['pairs']:
        if (p in seen)!=(q in seen): return None
        if p not in seen: continue
        ride=seen[q]-seen[p]-nodes[p]['service']; direct=t[p][q]
        if route.index(p)>=route.index(q) or ride>direct+P['detour'] or ride<0: return None
        metrics.append({'request_id':nodes[p]['request_id'],'pickup_sec':seen[p],'dropoff_sec':seen[q],'ride_sec':ride,'direct_sec':direct,'detour_sec':max(0,ride-direct),'pickup_delay_sec':seen[p]-nodes[p]['lo']})
    return {'route':route,'times':times,'distance_m':sum(d[i][j] for i,j in zip(route,route[1:])),'duration_sec':times[-1],'served':metrics}

def greedy(P, rows):
    # FCFS booking order, pick up and drop off in that same order. Reject if infeasible.
    chosen=[]; end=len(P['nodes'])-1
    for k in sorted(range(len(rows)),key=lambda k:(int(rows[k]['booked_sec']),rows[k]['request_id'])):
        trial=chosen+[P['pairs'][k]]
        route=[0]+[p for p,q in trial]+[q for p,q in trial]+[end]
        if evaluate(P,route): chosen=trial
    route=[0]+[p for p,q in chosen]+[q for p,q in chosen]+[end]
    return evaluate(P,route,allow_empty_overrun=True)

def optimize(P, rows, seconds):
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    n=len(P['nodes']); H=P['H']; nodes=P['nodes']; m=pywrapcp.RoutingIndexManager(n,1,[0],[n-1]); r=pywrapcp.RoutingModel(m)
    idx=m.NodeToIndex; node=m.IndexToNode
    dcb=r.RegisterTransitCallback(lambda i,j:P['d'][node(i)][node(j)])
    tcb=r.RegisterTransitCallback(lambda i,j:nodes[node(i)]['service']+P['t'][node(i)][node(j)])
    qcb=r.RegisterUnaryTransitCallback(lambda i:nodes[node(i)]['q'])
    r.SetArcCostEvaluatorOfAllVehicles(dcb); r.AddDimension(tcb,H,H,True,'Time'); r.AddDimensionWithVehicleCapacity(qcb,0,[P['capacity']],True,'Load')
    td=r.GetDimensionOrDie('Time'); s=r.solver(); penalty=(n+1)*max(max(x) for x in P['d'])+1
    for p,q in P['pairs']:
        pi,qi=idx(p),idx(q)
        for k,i in [(p,pi),(q,qi)]:
            td.CumulVar(i).SetRange(nodes[k]['lo'],nodes[k]['hi']); r.AddVariableMinimizedByFinalizer(td.CumulVar(i))
        r.AddDisjunction([pi],penalty); r.AddDisjunction([qi],0)
        r.AddPickupAndDelivery(pi,qi); s.Add(r.ActiveVar(pi)==r.ActiveVar(qi)); s.Add(r.VehicleVar(pi)==r.VehicleVar(qi))
        delta=td.CumulVar(qi)-td.CumulVar(pi)
        # Conditional ride constraints for served pairs only.
        s.Add(delta>=nodes[p]['service']-2*H*(1-r.ActiveVar(pi)))
        s.Add(delta<=nodes[p]['service']+P['t'][p][q]+P['detour']+2*H*(1-r.ActiveVar(pi)))
    for p,q in P['pairs']:
        for pp,qq in P['pairs']: r.NextVar(idx(q)).RemoveValue(idx(pp))
    r.AddVariableMinimizedByFinalizer(td.CumulVar(r.End(0)))
    params=pywrapcp.DefaultRoutingSearchParameters(); params.first_solution_strategy=routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION; params.local_search_metaheuristic=routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH; params.time_limit.FromSeconds(seconds)
    sol=r.SolveWithParameters(params)
    baseline=greedy(P,rows)
    if not sol: return baseline,'fallback_no_solution'
    route=[]; times=[]; i=r.Start(0)
    while not r.IsEnd(i):
        route.append(node(i)); times.append(sol.Value(td.CumulVar(i))); i=sol.Value(r.NextVar(i))
    route.append(node(i)); times.append(sol.Value(td.CumulVar(i)))
    candidate=evaluate(P,route,times)
    if not candidate: return baseline,'fallback_validation'
    # Keep best known feasible candidate by primary served count, secondary distance.
    key=lambda z:(-len(z['served']),z['distance_m'])
    if key(baseline)<key(candidate): return baseline,'fallback_better_baseline'
    return candidate,'ortools_feasible'
