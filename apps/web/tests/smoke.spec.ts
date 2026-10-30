import {test,expect} from '@playwright/test';
test('all screens, keyboard navigation and capability editing',async({page})=>{
 const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('/');await expect(page.getByRole('heading',{name:'Build the story.'})).toBeVisible();
 await page.getByRole('button',{name:'Web',exact:true}).first().click();
 await page.getByRole('tab',{name:'Web performance'}).click();await page.getByRole('button',{name:'Add segment'}).click();
 await expect(page.getByLabel('CLS', {exact:true})).toBeDisabled();await expect(page.getByLabel('INP', {exact:true})).toBeDisabled();
 await page.getByRole('button',{name:'Connections',exact:true}).click();await expect(page.getByRole('heading',{name:'Connect the complete journey.'})).toBeVisible();
 await page.getByRole('button',{name:'Add app target',exact:true}).click();await expect(page.getByRole('dialog')).toBeVisible();await page.keyboard.press('Escape');
 await page.getByRole('button',{name:'Settings',exact:true}).click();await expect(page.getByRole('heading',{name:'Access, kept under control.'})).toBeVisible();
 await page.getByRole('button',{name:'Create token',exact:true}).click();await page.getByLabel('Token name',{exact:true}).fill('Browser check');await page.getByRole('dialog').getByRole('button',{name:'Create token',exact:true}).click();await expect(page.getByText('Token created',{exact:true})).toBeVisible();await page.getByRole('button',{name:'Done',exact:true}).click();
 await page.getByRole('button',{name:'Runs & streams',exact:true}).click();await expect(page.getByRole('heading',{name:'Traffic with visible outcomes.'})).toBeVisible();
 await page.keyboard.press(process.platform==='darwin'?'Meta+k':'Control+k');await page.getByPlaceholder('Search pages, presets and scenarios…').fill('Scenario studio');await page.keyboard.press('Enter');await expect(page.getByRole('heading',{name:'Build the story.'})).toBeVisible();
 expect(errors).toEqual([]);
});
test('save, inspect real preview, confirm a bounded dry run, observe receipts',async({page,request})=>{
 const caps=await (await request.get('/api/capabilities')).json();const s=caps.default_spec;s.name='Browser acceptance';s.traffic={mode:'burst',count:4,window:{last_minutes:5}};
 const saved=await (await request.post('/api/specs',{data:s})).json();
 await page.goto('/');await page.getByLabel('Saved scenarios',{exact:true}).selectOption(saved.id);
 await page.getByRole('button',{name:'Preview scenario',exact:true}).click();await expect(page.getByText('Payload linter passed',{exact:true})).toBeVisible();
 await page.getByRole('button',{name:'Configure run',exact:true}).click();await expect(page.getByRole('dialog')).toBeVisible();await expect(page.getByRole('button',{name:'Start dry run',exact:true})).toBeDisabled();
 await page.getByRole('checkbox',{name:'I reviewed this scenario and its local output volume.'}).check();await page.getByRole('button',{name:'Start dry run',exact:true}).click();
 await expect(page.getByRole('heading',{name:'Traffic with visible outcomes.'})).toBeVisible();await expect(page.locator('.run-item').first()).toContainText('completed',{timeout:30000});
 await expect(page.getByText('Dry run',{exact:true}).first()).toBeVisible();await expect(page.getByText('Delivery errors',{exact:true})).toBeVisible();
 await page.getByRole('button',{name:'Verify run',exact:true}).click();await expect(page.getByText('Add an Embrace API token or run these read-only MCP calls and attach the results.',{exact:true})).toBeVisible();
});
test('laptop layout has no horizontal overflow and keeps copilot disabled without a key',async({page})=>{
 await page.setViewportSize({width:1280,height:900});await page.goto('/');await expect(page.getByRole('heading',{name:'Connect your model'})).toBeVisible();await expect(page.getByLabel('Message Copilot')).toBeDisabled();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy();
 await page.getByRole('button',{name:'Toggle Copilot'}).click();await page.getByRole('tab',{name:'Population & traffic'}).click();await expect(page.getByLabel('Traffic mode',{exact:true})).toBeVisible();
});
test('copilot reviews a proposal before applying a new version',async({page,request})=>{
 const caps=await(await request.get('/api/capabilities')).json();const saved=await(await request.post('/api/specs',{data:caps.default_spec})).json();
 await page.route('**/api/settings',route=>route.fulfill({json:{secrets:{anthropic:true},defaults:{},models:['claude-sonnet-5-5']}}));
 await page.route('**/api/copilot/turns',async route=>{if(route.request().method()==='POST')await route.fulfill({json:{id:'recorded-proposal',summary:'Rename the scenario for the product journey.',patch:[{op:'replace',path:'/name',value:'Product journey'}]}});else await route.continue()});
 await page.route('**/api/copilot/turns/recorded-proposal/apply',route=>route.fulfill({json:{...saved,version:2,spec:{...saved.spec,name:'Product journey'}}}));
 await page.goto('/');await page.getByLabel('Saved scenarios',{exact:true}).selectOption(saved.id);await page.getByLabel('Message Copilot').fill('Rename this scenario Product journey');await page.getByRole('button',{name:'Send message'}).click();
 await expect(page.getByText('Proposed scenario change')).toBeVisible();await expect(page.getByLabel('Scenario name')).not.toHaveValue('Product journey');
 await page.getByRole('button',{name:'Apply',exact:true}).click();await expect(page.getByLabel('Scenario name')).toHaveValue('Product journey');
});
test('preparation plan stays local until its reviewed confirmation',async({page,request})=>{
 await page.goto('/');await page.getByRole('button',{name:'Plan preparation',exact:true}).click();await page.getByLabel('History hours',{exact:true}).fill('0');await page.getByLabel('Live duration (minutes)',{exact:true}).fill('1');await page.getByRole('button',{name:'Review preparation plan'}).click();
 await expect(page.getByText('Exact scenarios and projected evidence')).toBeVisible();await expect(page.getByRole('button',{name:'Start preparation',exact:true})).toBeDisabled();
 const plans=await(await request.get('/api/preparations')).json();expect(plans[0].status).toBe('planned');await page.keyboard.press('Escape');
});
