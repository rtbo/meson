project('dub-example', 'd')

dub_exe = find_program('dub', required : false)
if not dub_exe.found()
    error('MESON_SKIP_TEST: Dub not found')
endif

if meson.get_compiler('d').get_id() == 'gcc'
    error('MESON_SKIP_TEST: can\'t build dependencies with GDC')
elif meson.get_compiler('d').get_id() == 'llvm'
    dc = 'ldc2'
elif meson.get_compiler('d').get_id() == 'dmd'
    dc = 'dmd'
endif

arch = host_machine.cpu_family()
if arch == 'x86' and host_machine.system() == 'windows'
    arch = 'x86_mscoff'
endif

run_command('dub', 'run', 'dub-build-deep', '--yes', '--', 'xlsx', '--compiler', dc, '--arch', arch,
    check: true,
)

xlsx_dep = dependency('xlsx', method: 'dub')

test_exe = executable('test-test6', 'test.d', dependencies: xlsx_dep)

test('test dub with deps', test_exe)
