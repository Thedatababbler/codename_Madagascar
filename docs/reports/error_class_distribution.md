# Error-class distribution of recorded persistent failures

Runs scanned: `outputs/*/*/*/tasks/*/task_execution.json`; 85 searched milestones with a persistent set.

| class | persistent cases | milestones where it is the main class |
|---|---|---|
| E1 | 0 | 0 |
| E2 | 31 | 9 |
| E3 | 131 | 39 |
| E4 | 60 | 10 |
| E5 | 37 | 7 |
| E6 | 27 | 5 |
| E7 | 60 | 12 |
| E8 | 0 | 0 |
| E9 | 0 | 16 |

Classes with fewer than 5 cases: E1, E8.
Suggested merges (confirm by hand before writing them into the config): E1 -> E3; E8 -> E1.

Rules that need the failure output (timeouts, DID NOT RAISE) did not fire in this offline pass;
E5 was reached from `pytest.raises` in the source alone, E8 not at all.

| run | milestone | main classes | counts |
|---|---|---|---|
| cpe-20260914T052228Z-tinydb | storage_and_utils_contracts | E3 | {'E3': 1} |
| cpe-20260914T180406Z-imapclient | transport_session_and_authentication | E3 | {'E3': 3} |
| cpe-20260914T180406Z-imapclient | mailbox_message_and_extension_operations | E3 | {'E3': 6, 'E6': 1} |
| cpe-20260914T180406Z-imapclient | public_api_config_and_full_integration | E7 | {'E3': 1, 'E7': 2} |
| cpe-20260914T212411Z-python-hl7 | hierarchical_containers | E9 | {'E5': 2, 'E6': 1, 'E3': 1} |
| cpe-20260915T014938Z-flask | routing_scaffold_and_blueprints | E3 | {'E3': 6} |
| cpe-20260915T050934Z-trailscraper | record_sources_and_downloads | E3 | {'E3': 2, 'E6': 1} |
| cpe-20260915T050934Z-trailscraper | policy_guessing | E7 | {'E7': 1} |
| cpe-20260915T083614Z-portalocker | shared_contracts | E3 | {'E3': 1} |
| cpe-20260915T083614Z-portalocker | redis_distributed_locking | E7, E3 | {'E7': 3, 'E3': 2} |
| cpe-20260915T083614Z-portalocker | public_api_cli_integration | E3 | {'E3': 1} |
| cpe-20260915T182714Z-nl2_tablib | shared_contracts_and_registry | E3 | {'E3': 1} |
| cpe-20260915T182714Z-nl2_tablib | text_dataframe_formats | E9 | {'E2': 4, 'E5': 1, 'E3': 2} |
| cpe-20260915T182714Z-nl2_tablib | public_api_cli_packaging_integration | E3 | {'E3': 3} |
| cpe-20260916T083207Z-nl2_tablib | text_dataframe_formats | E2 | {'E2': 2} |
| cpe-20260916T083207Z-nl2_tablib | office_and_dbf_formats | E2 | {'E3': 1, 'E2': 2} |
| cpe-20260916T083207Z-nl2_tablib | public_api_cli_packaging_integration | E2 | {'E2': 2, 'E3': 1} |
| cpe-20260916T122536Z-nl2_tenacity | strategy_primitives | E6 | {'E6': 1} |
| cpe-20260916T160528Z-nl2_tablib | public_api_cli_packaging_integration | E3 | {'E3': 1} |
| cpe-20260916T184425Z-nl2_python-pathspec | shared_contracts_and_utilities | E7, E3 | {'E7': 1, 'E3': 1} |
| cpe-20260902T202959Z | implement_client_behaviour_and_public_surface | E3 | {'E3': 7, 'E7': 1} |
| cpe-20260902T212653Z | crypto_and_jwk_contracts | E3 | {'E3': 3} |
| cpe-20260902T222657Z | freeze_storage_and_node_contracts | E6, E3 | {'E6': 1, 'E3': 1} |
| cpe-20260903T053028Z | freeze_shared_api_contracts | E9 | {'E6': 1, 'E3': 2, 'E7': 2} |
| cpe-20260903T053028Z | implement_client_behaviour_and_public_surface | E3 | {'E3': 4, 'E7': 1, 'E6': 1} |
| cpe-20260903T063913Z | crypto_and_jwk_contracts | E3 | {'E3': 3, 'E2': 1} |
| cpe-20260903T072832Z | freeze_storage_and_node_contracts | E5 | {'E4': 1, 'E5': 2} |
| cpe-20260904T024603Z | crypto_and_jwk_contracts | E3 | {'E3': 1} |
| cpe-20260904T062616Z | freeze_storage_and_node_contracts | E4 | {'E4': 6, 'E5': 2} |
| cpe-20260904T074822Z | freeze_storage_and_node_contracts | E5, E4 | {'E5': 2, 'E4': 2} |
| cpe-20260904T084211Z | freeze_storage_and_node_contracts | E9 | {'E7': 2, 'E5': 3, 'E4': 1} |
| cpe-20260904T115649Z | deliver_bplustree_behavior_and_public_api | E4 | {'E4': 2, 'E7': 1} |
| cpe-20260904T120045Z | deliver_bplustree_behavior_and_public_api | E4 | {'E4': 4} |
| cpe-20260904T130843Z | freeze_storage_and_node_contracts | E7, E5 | {'E7': 1, 'E5': 1} |
| cpe-20260904T135751Z | freeze_storage_and_node_contracts | E7, E4 | {'E7': 1, 'E4': 1} |
| cpe-20260904T135751Z | deliver_bplustree_behavior_and_public_api | E7 | {'E7': 6, 'E4': 1} |
| cpe-20260904T150432Z | freeze_storage_and_node_contracts | E5, E4 | {'E5': 1, 'E4': 1} |
| cpe-20260906T070812Z | deliver_bplustree_behavior_and_public_api | E4 | {'E4': 1} |
| cpe-20260906T172236Z | implement_client_behaviour_and_public_surface | E9 | {'E3': 4, 'E7': 4, 'E6': 1} |
| cpe-20260906T184126Z | implement_client_behaviour_and_public_surface | E9 | {'E3': 3, 'E6': 1, 'E7': 1} |
| cpe-20260907T001908Z | implement_client_behaviour_and_public_surface | E3 | {'E3': 2, 'E7': 1} |
| cpe-20260907T011421Z | freeze_storage_and_node_contracts | E6 | {'E6': 1} |
| cpe-20260907T072100Z | deliver_bplustree_behavior_and_public_api | E4, E7 | {'E7': 3, 'E4': 5} |
| cpe-20260908T055301Z | freeze_storage_and_node_contracts | E5 | {'E5': 1} |
| cpe-20260908T055301Z | deliver_bplustree_behavior_and_public_api | E7 | {'E7': 1} |
| cpe-20260908T071436Z | implement_client_behaviour_and_public_surface | E9 | {'E6': 1, 'E3': 3, 'E7': 2} |
| cpe-20260908T081547Z | crypto_and_jwk_contracts | E9 | {'E5': 6, 'E6': 1, 'E2': 3, 'E3': 4} |
| cpe-20260908T081547Z | token_and_jwks_behaviour | E9 | {'E7': 2, 'E3': 1, 'E5': 1, 'E6': 1} |
| cpe-20260908T105137Z | implement_csvs_to_sqlite | E3 | {'E3': 2} |
| cpe-20260908T155355Z | core_hl7_contracts | E5 | {'E5': 2, 'E7': 1} |
| cpe-20260908T193943Z | implement_python_hl7 | E3 | {'E3': 2, 'E4': 1} |
| cpe-20260908T224234Z | implement_voluptuous_library | E9 | {'E7': 1, 'E6': 1, 'E5': 1} |
| cpe-20260908T234833Z | implement_zxcvbn_library | E3 | {'E3': 3, 'E6': 1} |
| cpe-20260909T024338Z | implement_emoji_processing | E3 | {'E3': 1} |
| cpe-20260909T040613Z | public_contract_and_substrate | E3 | {'E3': 3, 'E7': 1} |
| cpe-20260909T065906Z | core_contracts_public_surface | E2 | {'E2': 1} |
| cpe-20260909T203648Z | deliver_bplustree_behavior_and_public_api | E4 | {'E4': 13, 'E5': 1} |
| cpe-20260909T212730Z | freeze_public_contracts | E2, E3 | {'E2': 1, 'E3': 1} |
| cpe-20260909T212730Z | implement_unicode_repair_library | E3 | {'E3': 3} |
| cpe-20260909T225142Z | core_async_wrapper_contracts | E9 | {'E3': 4, 'E6': 3, 'E4': 4, 'E5': 2, 'E2': 2, 'E7': 4} |
| cpe-20260909T234006Z | shared_jose_contracts | E9 | {'E2': 1, 'E3': 1, 'E6': 1} |
| cpe-20260910T001641Z | public_contract_and_substrate | E3 | {'E3': 1} |
| cpe-20260910T001641Z | matching_behaviour_and_gitignore_integration | E3 | {'E3': 1} |
| cpe-20260910T011045Z | core_contracts_and_public_api | E3 | {'E3': 1} |
| cpe-20260910T011045Z | complete_validation_toolkit | E7, E5 | {'E5': 2, 'E7': 3} |
| cpe-20260910T030207Z | core_async_wrapper_contracts | E2, E3 | {'E3': 3, 'E2': 4} |
| cpe-20260910T030904Z | deliver_bplustree_behavior_and_public_api | E6 | {'E6': 1} |
| cpe-20260910T044948Z | freeze_emoji_contracts | E6 | {'E6': 1} |
| cpe-20260910T053758Z | matching_behaviour_and_gitignore_integration | E9 | {'E5': 1, 'E3': 1, 'E7': 1} |
| cpe-20260910T065341Z | deliver_bplustree_behavior_and_public_api | E4 | {'E4': 17} |
| cpe-20260910T074320Z | public_contract_and_substrate | E9 | {'E5': 1, 'E3': 1, 'E7': 2} |
| cpe-20260910T074320Z | matching_behaviour_and_gitignore_integration | E3 | {'E3': 2} |
| cpe-20260910T080522Z | core_async_wrapper_contracts | E9 | {'E5': 1, 'E6': 1, 'E2': 2, 'E7': 1} |
| cpe-20260910T080522Z | complete_aiofiles_capabilities | E2 | {'E2': 1} |
| cpe-20260910T091543Z | core_contracts_and_registry | E3 | {'E3': 1} |
| cpe-20260910T105535Z | core_contracts_public_surface | E9 | {'E2': 2, 'E7': 6, 'E5': 4, 'E3': 7, 'E6': 2} |
| cpe-20260910T105535Z | complete_retry_behaviour | E3, E2 | {'E3': 1, 'E2': 1} |
| cpe-20260910T110049Z | matching_behaviour_and_gitignore_integration | E3 | {'E3': 5} |
| cpe-20260910T134807Z | shared_jose_contracts | E3 | {'E3': 2} |
| cpe-20260910T195252Z | freeze_storage_and_node_contracts | E3 | {'E3': 1} |
| cpe-20260910T225326Z | core_async_wrapper_contracts | E3 | {'E6': 2, 'E3': 7, 'E2': 1} |
| cpe-20260910T225326Z | complete_aiofiles_capabilities | E2 | {'E2': 1} |
| cpe-20260911T021924Z | public_contract_and_substrate | E7 | {'E7': 3, 'E3': 1} |
| cpe-20260901T072336Z | implement_client_behaviour_and_public_surface | E3 | {'E3': 3, 'E6': 1} |
| cpe-20260902T073231Z | implement_client_behaviour_and_public_surface | E7, E3 | {'E7': 1, 'E3': 1} |
