import pytest
from sql_agent.mcp_server import dispatch

@pytest.mark.parametrize('params',[None,[1],1,'invalid'])
def test_malformed_tool_params_do_not_terminate_server(params):
    result=dispatch({'jsonrpc':'2.0','id':7,'method':'tools/call','params':params})
    assert result['id']==7 and result['error']['code']==-32602
    assert dispatch({'jsonrpc':'2.0','id':8,'method':'ping'})['result']=={}


def test_stdio_survives_bad_tool_call_and_bad_json():
    import json
    import subprocess
    import sys
    messages=[json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call','params':[1]}),
              'not JSON',json.dumps({'jsonrpc':'2.0','id':2,'method':'ping'})]
    process=subprocess.run([sys.executable,'-m','sql_agent.mcp_server'],
        input='\n'.join(messages)+'\n',text=True,capture_output=True,timeout=10)
    assert process.returncode==0,process.stderr
    responses=[json.loads(line) for line in process.stdout.splitlines()]
    assert responses[0]['error']['code']==-32602
    assert responses[1]['error']['code']==-32700
    assert responses[2]['id']==2 and responses[2]['result']=={}
