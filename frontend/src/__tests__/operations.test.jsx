import {afterEach,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import Operations from '../components/Operations';
import {api} from '../api';
vi.mock('../api',()=>({api:{opsSummary:vi.fn(),opsIncidents:vi.fn(),opsAlerts:vi.fn(),opsUpdate:vi.fn(),opsRetry:vi.fn()}}));
afterEach(()=>{cleanup();vi.resetAllMocks();});
it('shows missing delivery honestly and prevents resolution while the condition is active',async()=>{
  api.opsSummary.mockResolvedValue({delivery:'not_configured'});api.opsIncidents.mockResolvedValue([{incident_id:'case1',kind:'unknown_payment',state:'open',version:2,condition_active:true}]);api.opsAlerts.mockResolvedValue([]);
  render(<Operations/>);
  expect((await screen.findByText(/Events are retained locally/)).textContent).toContain('no external notification');
  fireEvent.change(screen.getByLabelText('Reason for next case action'),{target:{value:'Investigating'}});
  expect(screen.getByRole('button',{name:'Resolve case1'}).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button',{name:'Acknowledge case1'}));
  await waitFor(()=>expect(api.opsUpdate).toHaveBeenCalledWith('case1',{expected_version:2,state:'acknowledged',reason:'Investigating'}));
});
