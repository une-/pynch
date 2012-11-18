import sys
import ast
import tokenize
import subprocess
import distutils.core
import shutil
import os


class CInsert(object):
    def __init__(self, py_line_number, indentation, step_down, index, lines):
        # line number that this insert should come before
        self.py_line_number = py_line_number
        # indentation that the python interface should conform to
        self.indentation = indentation
        # whether or not the insert dedents fromt he previous line
        self.step_down = step_down
        # the index of the indent
        self.index = index
        # the lines of c represented by this indent
        self.lines = lines

    def __repr__(self):
        return (self.py_line_number, self.indentation, self.step_down, self.index, self.lines)
    def __str__(self): return repr(self)


def flatten(l): return [item for sublist in l for item in sublist]

def flatten_ast(node):
    result = []
    if not hasattr(node, 'body'): return result
    for child in node.body:
        result.append(child)
        result += flatten_ast(child)
    return result

def split_file(f, keep_temp):
    module_name = f[:f.index('.c.py')]
    # try to create a directory to store our temporary files
    temp_dir = ''
    for i in range(500):
        if not os.path.exists('{0}_temp{1}'.format(module_name, i)):
            temp_dir = '{0}_temp{1}'.format(module_name, i)
            os.mkdir(temp_dir)
            break
    if temp_dir == '': return
    # open files for reading and writing
    c_py_file = open(f, 'r')
    py_file = open(module_name + '.py', 'w')
    c_file = open(os.path.join(temp_dir, module_name + '.c'), 'w')
    setup_file = open(os.path.join(temp_dir, module_name + '_setup.py'), 'w')
    
    # the lines which will be translated to the pure python file
    py_lines = []
    # whether the next character can complete {% or %}
    expect_c = False
    # whether or not the current line is a line of c
    in_c = False
    # whether or not the current line should be ignored as code (for lines with %})
    ignore_line = False
    # the current line number
    line = 1
    # the indentation of the previous line (for CInsert.step_down)
    prev_indent = ''
    # the stack of indentation levels (for CInsert.indentation)
    indentation = ['']
    # the list of CInserts for this file
    insertion_points = []
    for t in tokenize.generate_tokens(open(f, 'r').readline):
        # on { we know to expect % to begin a c block
        if t[0] == tokenize.OP and t[1] == '{' and not in_c:
            expect_c = True
            continue
        # on % we know to expect } if we are in a c block
        # or to start a c block if the last token was {
        if t[0] == tokenize.OP and t[1] == '%':
            if expect_c:
                expect_c = False
                in_c = True
                ignore_line = True
                insertion_points.append(CInsert(line, indentation[-1], indentation[-1] < prev_indent, len(insertion_points), []))
            elif in_c:
                expect_c = True
            continue
        # on } we know to end a c block if the last token was % and we are in a c block
        if t[0] == tokenize.OP and t[1] == '}' and in_c and expect_c:
            expect_c = False
            in_c = False
            ignore_line = True
            continue
        # on newline we know to
        # add the line to py_lines if we're not in a c block
        # or add the line to the current CInsert's lines if we are
        if t[0] == tokenize.NL or t[0] == tokenize.NEWLINE:
            py_line = c_py_file.readline()
            if not ignore_line:
                if not in_c:
                    py_lines.append(py_line)
                    prev_indent = indentation[-1]
                    line += 1
                else:
                    insertion_points[-1].lines.append(py_line)
            ignore_line = False
        # on indent we need to push the new indentation level to our indentation stack
        if t[0] == tokenize.INDENT:
            indentation.append(t[1])
        # on dedent we need to pop an indentation level from our indentation stack
        if t[0] == tokenize.DEDENT:
            indentation.pop()
        # if we made it this far expecting the end to a c block token, we can give up
        if expect_c: expect_c = False

    # parse the entire file in to a code object
    cu = ast.parse(''.join(py_lines), module_name + '.py')

    # c file foreword
    c_file.write('#include <Python.h>\n')

    # number of pure python lines we've written
    py_lines_written = 0
    # the stack of names in scope, start with no names in the bare scope
    names = [[]]
    # the stack of code nodes we are iterating through
    nodes = [(0, e) for e in cu.body[::-1]]
    # the stack of 
    scope_levels = [-1]
    last_level = 0
    for insert in insertion_points:
        node = None
        while len(nodes) > 0:
            level, node = nodes.pop()

            #if node.lineno >= insert[0]:
            if node.lineno >= insert.py_line_number:
                nodes.append((level, node))
                break
            
            if level <= scope_levels[-1]:
                scope_levels.pop()
                names.pop()
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    names[-1].append(t.id)
            if isinstance(node, ast.FunctionDef):
                names.append([])
                scope_levels.append(level)
                for a in node.args.args:
                    names[-1].append(a.id)
            
            last_level = level
            if hasattr(node, 'body'):
                nodes.extend([(level + 1, e) for e in node.body[::-1]])
        
        # write py_file lines up to this point
        lines_to_write = None
        if len(nodes) > 0: lines_to_write = py_lines[py_lines_written:nodes[-1][1].lineno - 1]
        else: lines_to_write = py_lines[py_lines_written:]
        for line in lines_to_write:
            py_file.write(line)
            py_lines_written += 1
        
        #if insert[2]: names.pop()
        if insert.step_down: names.pop()
        writeable_names = names[-1] if len(names) else []
        readable_names = flatten(names)
        
        # write python-to-c section
        if len(readable_names) > 0:
            py_file.write('{0}import {1}_c\n'.format(insert.indentation, module_name))
            py_file.write('{0}r = {1}_c.f{2}({3})\n'.format(insert.indentation, module_name, insert.index, ', '.join(readable_names)))
            py_file.write('{0}{1} = {2}\n'.format(insert.indentation, ', '.join(writeable_names), ', '.join(['r[{0}]'.format(i) for i in range(len(writeable_names))])))
        else:
            py_file.write('{0}import {1}_c\n'.format(insert.indentation, module_name))
            py_file.write('{0}{1}_c.f{2}({3})\n'.format(insert.indentation, module_name, insert.index, ', '.join(readable_names)))
        
        # write c function foreword
        c_file.write('static PyObject* f{0}(PyObject* self, PyObject* args) {{\n'.format(insert.index))
        for i in range(len(readable_names)):
            c_file.write('\tPyObject* {0} = PyTuple_GetItem(args, {1});\n'.format(readable_names[i], i))
        
        # write user c code
        c_file.write(''.join(insert.lines))
        
        # write c function afterword
        if len(writeable_names) > 0:
            c_file.write('\treturn Py_BuildValue("({0})", {1});\n'.format('O' * len(writeable_names), ', '.join(writeable_names)))
        c_file.write('}\n')
    
    # finish writing py_file lines
    for line in py_lines[py_lines_written:]: py_file.write(line)

    # c file afterword
    # function bindings
    c_file.write('static PyMethodDef {0}Methods[] = {{\n'.format(module_name))
    for insert in insertion_points:
        c_file.write('\t{{"f{0}", f{0}, METH_VARARGS, ""}},\n'.format(insert.index))
    c_file.write('\t{NULL, NULL, 0, NULL}\n};\n')
    # init function
    c_file.write('PyMODINIT_FUNC init{0}_c(void) {{\n'.format(module_name))
    c_file.write('\tPyObject* mod = Py_InitModule("{0}_c", {0}Methods);\n'.format(module_name))
    c_file.write('\tif (mod == NULL) return;\n}\n')

    setup_file.write('from distutils.core import setup, Extension\n')
    setup_file.write("{0}_module = Extension('{0}_c', sources = ['{0}.c'])\n".format(module_name))
    setup_file.write("setup(name = '{0}_c', ext_modules = [{0}_module])\n".format(module_name))
    
    # close files
    c_py_file.close()
    py_file.close()
    c_file.close()
    setup_file.close()

    # compile
    cd = os.getcwd()
    os.chdir(temp_dir)
    distutils.core.run_setup(module_name + '_setup.py', script_args = ['build'])
    os.chdir(cd)
    
    # copy
    d = ''
    for item in os.listdir(os.path.join(temp_dir, 'build')):
        if item.startswith('lib.'):
            d = os.path.join(temp_dir, 'build', item)
            break
    df = module_name + '_c.so'
    shutil.copyfile(os.path.join(d, df), df)
    shutil.copymode(os.path.join(d, df), df)
    if not keep_temp: shutil.rmtree(temp_dir)


def main():
    files = sys.argv[1:]
    
    for f in files:
        split_file(f, False)

if __name__ == '__main__': main()
