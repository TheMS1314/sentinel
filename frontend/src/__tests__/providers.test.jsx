import {afterEach,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import ProviderConnections from '../components/ProviderConnections';
import {api} from '../api';
vi.mock('../api',()=>({api:{providers:vi.fn(),bindProvider:vi.fn(),refreshProvider:vi.fn()}}));
afterEach(()=>{cleanup();vi.resetAllMocks();});
it('does not confuse configured credentials with verified commercial evidence',async()=>{
 api.providers.mockResolvedValue({configuration:{recorded_future:'configured_unverified',riskrecon:'not_configured'},bindings:[]});
 render(<ProviderConnections/>);
 expect(await screen.findByText('recorded_future: configured_unverified')).toBeTruthy();
 expect(screen.getByText(/licensed access/)).toBeTruthy();
});
it('shows a missing provider credential after refresh without reporting success',async()=>{
 api.providers.mockResolvedValue({configuration:{recorded_future:'not_configured'},bindings:[{binding_id:'b1',merchant_id:'m1',provider:'recorded_future',external_id:'idn:example.com',version:1}]});
 api.refreshProvider.mockResolvedValue({status:'not_configured',error:'Provider credential missing'});
 render(<ProviderConnections/>);
 fireEvent.click(await screen.findByRole('button',{name:'Refresh b1'}));
 await waitFor(()=>expect(api.refreshProvider).toHaveBeenCalledWith('b1'));
 expect((await screen.findByRole('status')).textContent).toContain('not_configured');
});
