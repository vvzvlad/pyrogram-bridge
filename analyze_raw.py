#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import os
import json

def parse_file(filename):
    with open(filename, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Ищем определение класса PostParser
    class_match = re.search(r'class\s+PostParser[^{]*?:', content)
    if not class_match:
        print("Класс PostParser не найден в файле.")
        return {}
    
    class_start = class_match.start()
    
    # Находим все методы класса
    methods = {}
    method_pattern = re.compile(r'def\s+([a-zA-Z0-9_]+)\s*\([^)]*self[^)]*\)[^:]*:')
    
    for method_match in method_pattern.finditer(content, class_start):
        method_name = method_match.group(1)
        method_start = method_match.start()
        
        # Находим конец метода (начало следующего метода или конец класса)
        next_method_match = method_pattern.search(content, method_match.end())
        if next_method_match:
            method_end = next_method_match.start()
        else:
            # Если это последний метод, ищем конец файла
            method_end = len(content)
        
        method_body = content[method_start:method_end]
        
        # Ищем вызовы других методов через self
        calls = re.findall(r'self\.([a-zA-Z0-9_]+)\s*\(', method_body)
        calls = list(set([call for call in calls if call != method_name and not call.startswith('__')]))
        
        methods[method_name] = calls
    
    return methods

def export_graphviz(methods, filename='method_calls.dot'):
    with open(filename, 'w') as f:
        f.write('digraph G {\n')
        f.write('  node [shape=box, style=filled, fillcolor=lightblue];\n')
        
        # Добавляем узлы
        for method in methods:
            f.write(f'  "{method}" [label="{method}"];\n')
        
        # Добавляем связи
        for method, calls in methods.items():
            for call in calls:
                if call in methods:  # Проверяем, что вызываемый метод существует
                    f.write(f'  "{method}" -> "{call}";\n')
        
        f.write('}\n')
    
    print(f"Граф сохранен в файле {filename}")
    print("Для генерации изображения выполните: dot -Tpng method_calls.dot -o method_calls.png")


if __name__ == "__main__":
    # Анализируем файл
    methods = parse_file('post_parser.py')
    
    # Подсчитываем общее количество методов и вызовов
    total_methods = len(methods)
    total_calls = sum(len(calls) for calls in methods.values())
    
    print(f"Обнаружено {total_methods} методов с {total_calls} вызовами.")
    
    
    # Экспортируем в формат Graphviz
    export_graphviz(methods)
    

    os.system('dot -Tpng method_calls.dot -o method_calls.png')
    os.system('rm method_calls.dot')
    os.system('open method_calls.png')