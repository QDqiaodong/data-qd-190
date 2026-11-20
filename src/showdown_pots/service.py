from .cards import best_seven
from .model import validate
from .pots import settle

def process(source):
    data=validate(source)
    hands={p['id']:best_seven(data['board']+p['hole']) for p in data['players'] if not p['folded']}
    pots,returns,payouts,refunds=settle(data['players'],hands,data['button_seat'])
    result=[{'player_id':p['id'],'seat':p['seat'],'committed':p['committed'],'pot_awards':payouts[p['id']],'uncalled_return':refunds[p['id']],
             'net':payouts[p['id']]+refunds[p['id']]-p['committed'],'hand':hands.get(p['id'])} for p in sorted(data['players'],key=lambda p:p['seat'])]
    return {'schema_version':1,'board':data['board'],'pots':pots,'uncalled_returns':returns,'players':result,
            'summary':{'players':len(result),'pots':len(pots),'total_committed':sum(p['committed'] for p in data['players']),
                       'total_awarded':sum(payouts.values()),'total_returned':sum(refunds.values()),'net_sum':sum(p['net'] for p in result)}}
