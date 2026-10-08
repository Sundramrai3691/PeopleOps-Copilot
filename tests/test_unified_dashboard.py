"""Real browser interaction with a deterministic HTTP transport; no model claims."""
import shutil

import pytest

from sql_agent.unified_dashboard import render_unified_dashboard


@pytest.mark.parametrize('status,complete', [('failed',False), ('needs_review',True), ('waiting_user',True)])
def test_usage_and_clarification_are_visible_and_actionable(status, complete):
    playwright = pytest.importorskip('playwright.sync_api')
    chrome = shutil.which('google-chrome')
    if not chrome:
        pytest.skip('Chrome is required')
    cost = {'complete':complete, 'input_tokens':10, 'output_tokens':4,
            'total_tokens':14, 'observed_total_tokens':14}
    evidence = {'telemetry':{'token_cost':cost},
                'clarification':{'id':'question-1','question':'Which order?'}}
    job = {'job_id':'job-1','status':status,'result':None,'error':None}
    if status == 'failed':
        job['error'] = {'evidence':evidence}
    else:
        job['result'] = evidence
    replies = []
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def route(request):
            path = request.request.url.split('http://127.0.0.1:49998',1)[-1]
            if path == '/':
                request.fulfill(content_type='text/html',body=render_unified_dashboard())
                return
            if path == '/v1/auth/session':
                value = {'name':'tester','role':'admin'}
            elif path == '/v1/sources':
                value = {'tasks':[], 'databases':[{'database_id':'demo','engine':'sqlite'}]}
            elif path.endswith('/resume'):
                replies.append(request.request.post_data_json)
                job['status'] = 'needs_review'
                value = {'job':job}
            else:
                value = job
            request.fulfill(json=value)
        page.route('**/*',route)
        try:
            page.goto('http://127.0.0.1:49998/')
            page.wait_for_selector('#workspace',state='visible')
            page.fill('#request-id','job-1')
            page.click('#load')
            page.wait_for_function("document.querySelector('#token-cost').textContent.includes('14')")
            text = page.locator('#token-cost').inner_text()
            assert ('total unknown' in text) is (not complete)
            if status == 'waiting_user':
                assert page.locator('#clarification-question').inner_text() == 'Which order?'
                page.fill('#clarification-answer','Order 1')
                page.click('#answer')
                page.wait_for_function("document.querySelector('#status').textContent === 'needs_review'")
                assert replies == [{'clarification_id':'question-1','answer':'Order 1'}]
            else:
                assert page.locator('#clarification-panel').is_hidden()
            assert not errors
        finally:
            browser.close()
