import {afterEach,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import MerchantDirectory from '../components/MerchantDirectory';
import {api} from '../api';
vi.mock('../api',()=>({api:{listMerchants:vi.fn(),saveMerchant:vi.fn()}}));
afterEach(()=>{cleanup();vi.resetAllMocks();});
it('submits owner-controlled identity and labels settlement as unverified',async()=>{
  api.listMerchants.mockResolvedValue([]);api.saveMerchant.mockResolvedValue({merchant_id:'mid_new'});
  render(<MerchantDirectory identity={{role:'owner',owner_id:'owner1'}}/>);
  const fields={'Registered merchant name':'Shop','Registered category':'grocery','Registered domain':'shop.example','Registration evidence/reference':'Human approval'};
  for(const [label,value] of Object.entries(fields))fireEvent.change(screen.getByLabelText(label),{target:{value}});
  fireEvent.click(screen.getByRole('button',{name:'Save merchant identity'}));
  await waitFor(()=>expect(api.saveMerchant).toHaveBeenCalledWith(expect.objectContaining({owner_id:'owner1',category:'grocery',expected_version:0})));
  expect((await screen.findByRole('status')).textContent).toContain('settlement identity remains unverified');
});
it('reviewers can view the directory but cannot edit it',async()=>{
  api.listMerchants.mockResolvedValue([]);render(<MerchantDirectory identity={{role:'reviewer',owner_id:'owner1'}}/>);
  await waitFor(()=>expect(api.listMerchants).toHaveBeenCalled());
  expect(screen.queryByRole('button',{name:'Save merchant identity'})).toBeNull();
});
