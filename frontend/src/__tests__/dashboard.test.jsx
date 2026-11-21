import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, act } from '@testing-library/react';
import App, { Dashboard } from '../App';
import { ToastProvider } from '../components/Toast';
import { api } from '../api';
vi.mock('../api',()=>({api:{me:vi.fn(), login:vi.fn(), listTransactionsPage:vi.fn(), summary:vi.fn(),
  listScenarioTags:vi.fn(), listPolicies:vi.fn(), getPolicy:vi.fn(), evalReport:vi.fn(), createPolicy:vi.fn()}}));
afterEach(()=>{cleanup();vi.resetAllMocks();});

it('shows sign in when no session exists and clears the entered credential after login', async()=>{
  api.me.mockRejectedValue(new Error('Sign in required'));
  api.login.mockResolvedValue({subject:'owner',role:'owner'});
  api.listTransactionsPage.mockResolvedValue({items:[],total_pages:1,total:0});
  api.summary.mockResolvedValue({total:0,by_verdict:{}});
  api.listScenarioTags.mockResolvedValue([]);api.listPolicies.mockResolvedValue([]);
  api.getPolicy.mockRejectedValue(new Error('missing'));api.evalReport.mockRejectedValue(new Error('missing'));
  render(<ToastProvider><App/></ToastProvider>);
  const input=await screen.findByLabelText('Access credential');
  fireEvent.change(input,{target:{value:'private-credential'}});
  fireEvent.click(screen.getByRole('button',{name:'Sign in'}));
  await screen.findByText(/Sign out/);
  expect(screen.queryByDisplayValue('private-credential')).toBeNull();
});

it('ignores old feed responses after a filter changes', async()=>{
  let resolveOld;
  api.listTransactionsPage.mockImplementationOnce(()=>new Promise(r=>resolveOld=r))
    .mockResolvedValue({items:[],total_pages:1,total:0});
  api.summary.mockResolvedValue({total:0,by_verdict:{}});
  api.listScenarioTags.mockResolvedValue([]);api.listPolicies.mockResolvedValue([]);
  api.getPolicy.mockResolvedValue({policy:{agent_id:'agent_demo_1'}});api.evalReport.mockRejectedValue(new Error('missing'));
  render(<ToastProvider><Dashboard identity={{subject:'owner',role:'owner'}} onLogout={()=>{}}/></ToastProvider>);
  fireEvent.click(screen.getByRole('button',{name:'BLOCK'}));
  await waitFor(()=>expect(api.listTransactionsPage).toHaveBeenCalledTimes(2));
  await act(async()=>resolveOld({items:[{request_id:'old',merchant_name:'Old merchant',merchant_category:'grocery',amount:1,verdict:'APPROVE'}],total_pages:1,total:1}));
  expect(screen.queryByText('Old merchant')).toBeNull();
});
