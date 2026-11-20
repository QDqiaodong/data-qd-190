from collections import Counter
from itertools import combinations

RANKS='23456789TJQKA'
LABELS=('high_card','pair','two_pair','three_of_a_kind','straight','flush','full_house','four_of_a_kind','straight_flush')

def card(value):
    if not isinstance(value,str) or len(value)!=2 or value[0] not in RANKS or value[1] not in 'cdhs':
        raise ValueError('card must be rank 23456789TJQKA plus suit cdhs')
    return value

def rank_five(cards):
    values=sorted((RANKS.index(c[0])+2 for c in cards),reverse=True)
    counts=Counter(values)
    groups=sorted(((n,v) for v,n in counts.items()),reverse=True)
    distinct=sorted(counts,reverse=True)
    straight=5 if distinct==[14,5,4,3,2] else distinct[0] if len(distinct)==5 and distinct[0]-distinct[-1]==4 else None
    flush=len({c[1] for c in cards})==1
    if straight and flush:return (8,straight)
    if groups[0][0]==4:return (7,groups[0][1],groups[1][1])
    if [g[0] for g in groups]==[3,2]:return (6,groups[0][1],groups[1][1])
    if flush:return (5,*values)
    if straight:return (4,straight)
    if groups[0][0]==3:return (3,groups[0][1],*sorted((v for v in counts if v!=groups[0][1]),reverse=True))
    pairs=sorted((v for v,n in counts.items() if n==2),reverse=True)
    if len(pairs)==2:return (2,*pairs,next(v for v,n in counts.items() if n==1))
    if len(pairs)==1:return (1,pairs[0],*sorted((v for v,n in counts.items() if n==1),reverse=True))
    return (0,*values)

def best_seven(cards):
    best=None;proof=None
    for subset in combinations(cards,5):
        rank=rank_five(subset);candidate=sorted(subset)
        if best is None or rank>best or (rank==best and candidate<proof):best,proof=rank,candidate
    return {'rank':list(best),'category':LABELS[best[0]],'best_five':proof}
