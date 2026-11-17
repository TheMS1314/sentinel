import {afterEach,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import AccessControl from '../components/AccessControl';
import {api} from '../api';
vi.mock('../api',()=>({api:{listCredentials:vi.fn(),changeCredential:vi.fn()}}));
afterEach(()=>{cleanup();vi.resetAllMocks();});
const row={subject:'robot',role:'agent',owner_id:'',agent_ids:['a'],version:2,active:true};
it('rotates a versioned credential and hides the one-time secret on request',async()=>{
  api.listCredentials.mockResolvedValue([row]);api.changeCredential.mockResolvedValue({credential:'synthetic-one-time-secret'});
  render(<AccessControl/>);
  fireEvent.click(await screen.findByRole('button',{name:'Rotate robot'}));
  await waitFor(()=>expect(api.changeCredential).toHaveBeenCalledWith({subject:'robot',action:'rotate',expected_version:2}));
  expect((await screen.findByLabelText('New credential')).textContent).toBe('synthetic-one-time-secret');
  fireEvent.click(screen.getByRole('button',{name:'Hide credential'}));
  expect(screen.queryByText('synthetic-one-time-secret')).toBeNull();
});
it('reports stale-version failures without showing a secret',async()=>{
  api.listCredentials.mockResolvedValue([row]);api.changeCredential.mockRejectedValue(new Error('Refresh metadata'));
  render(<AccessControl/>);fireEvent.click(await screen.findByRole('button',{name:'Revoke robot'}));
  expect((await screen.findByRole('alert')).textContent).toContain('Refresh metadata');
  expect(screen.queryByLabelText('New credential')).toBeNull();
});
it('prevents accidental self-rotation from discarding the only visible new key',async()=>{
  api.listCredentials.mockResolvedValue([row]);render(<AccessControl currentSubject="robot"/>);
  expect((await screen.findByRole('button',{name:'Rotate robot'})).disabled).toBe(true);
  expect(screen.getByRole('button',{name:'Revoke robot'}).disabled).toBe(true);
});
