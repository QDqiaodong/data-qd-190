def settle(players,hands,button):
    payouts={p['id']:0 for p in players};refunds={p['id']:0 for p in players}
    previous=0;pots=[];returns=[]
    for level in sorted({p['committed'] for p in players if p['committed']>0}):
        contributors=sorted((p for p in players if p['committed']>=level),key=lambda p:p['seat'])
        amount=(level-previous)*len(contributors)
        if len(contributors)==1:
            owner=contributors[0]['id'];refunds[owner]+=amount
            returns.append({'player_id':owner,'from_exclusive':previous,'to_inclusive':level,'amount':amount})
        else:
            eligible=[p for p in contributors if not p['folded']]
            if not eligible:raise ValueError('matched contribution layer has no eligible live player')
            best=max(tuple(hands[p['id']]['rank']) for p in eligible)
            winners=sorted((p for p in eligible if tuple(hands[p['id']]['rank'])==best),key=lambda p:(p['seat']-button-1)%9)
            quotient,remainder=divmod(amount,len(winners))
            awards={p['id']:quotient+int(i<remainder) for i,p in enumerate(winners)}
            for name,value in awards.items():payouts[name]+=value
            pots.append({'pot':len(pots)+1,'from_exclusive':previous,'to_inclusive':level,'amount':amount,'contributor_ids':[p['id'] for p in contributors],
                         'eligible_ids':[p['id'] for p in eligible],'winner_ids_in_odd_chip_order':[p['id'] for p in winners],'awards':awards})
        previous=level
    total=sum(p['committed'] for p in players)
    if sum(payouts.values())+sum(refunds.values())!=total:raise ValueError('internal chip conservation failed')
    return pots,returns,payouts,refunds
