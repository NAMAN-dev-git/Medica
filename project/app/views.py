from decimal import Decimal

import razorpay
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from .forms import LoginForm, RegistrationForm
from .models import Category, Order, OrderItem, Product


def _cart_items(request):
    cart_data = request.session.get('cart', {})
    products = Product.objects.filter(id__in=list(cart_data.keys())).select_related('category')
    items, total = [], Decimal('0')
    for product in products:
        quantity = int(cart_data.get(str(product.id), 0))
        item_total = product.price * quantity
        items.append({'product': product, 'quantity': quantity, 'previous_quantity': max(quantity - 1, 0), 'next_quantity': min(quantity + 1, 10), 'item_total': item_total})
        total += item_total
    return items, total


def home(request):
    cart_data = request.session.get('cart', {})
    featured = Product.objects.filter(is_featured=True)[:4]
    featured_products = [{'product': product, 'quantity': int(cart_data.get(str(product.id), 0))} for product in featured]
    safety = Product.objects.filter(
        Q(category__name__icontains='safety') |
        Q(name__icontains='sanitizer') |
        Q(name__icontains='mask')
    )[:4]
    safety_products = [{'product': product, 'quantity': int(cart_data.get(str(product.id), 0))} for product in safety]
    return render(request, 'home.html', {
        'featured_products': featured_products,
        'safety_products': safety_products,
        'categories': Category.objects.all()[:4],
    })


def services(request):
    return render(request, 'services.html')


def about(request):
    return render(request, 'about.html')


def product_list(request):
    products = Product.objects.select_related('category').all()
    search = request.GET.get('search', '').strip()
    category = request.GET.get('category', '').strip()
    if search:
        products = products.filter(name__icontains=search)
    if category:
        products = products.filter(category__slug=category)
    cart_data = request.session.get('cart', {})
    product_cards = [{'product': product, 'quantity': int(cart_data.get(str(product.id), 0))} for product in products]
    return render(request, 'products.html', {'products': product_cards, 'categories': Category.objects.all(), 'active_category': category, 'search': search})


def product_detail(request, slug):
    product = get_object_or_404(Product, slug=slug)
    quantity = request.session.get('cart', {}).get(str(product.id), 0)
    return render(request, 'product_detail.html', {'product': product, 'quantity': int(quantity)})


def cart(request):
    items, total = _cart_items(request)
    paid_order_ids = request.session.get('paid_order_ids', [])
    orders = Order.objects.filter(status='paid')
    if request.user.is_authenticated:
        orders = orders.filter(Q(user=request.user) | Q(id__in=paid_order_ids))
    else:
        orders = orders.filter(id__in=paid_order_ids)
    orders = orders.prefetch_related('items__product').order_by('-created_at')
    return render(request, 'cart.html', {
        'cart_items': items,
        'total': total,
        'orders': orders,
    })


def add_to_cart(request, pk):
    get_object_or_404(Product, id=pk)
    cart_data = request.session.get('cart', {})
    key = str(pk)
    cart_data[key] = min(int(cart_data.get(key, 0)) + 1, 10)
    if cart_data[key] == 10:
        messages.info(request, 'Maximum quantity is 10 per product.')
    request.session['cart'] = cart_data
    messages.success(request, 'Product added to your cart.')
    return redirect(request.META.get('HTTP_REFERER', 'product_list'))


def update_cart(request, pk):
    cart_data = request.session.get('cart', {})
    quantity = min(max(int(request.POST.get('quantity', 1)), 0), 10)
    if quantity:
        cart_data[str(pk)] = quantity
    else:
        cart_data.pop(str(pk), None)
    request.session['cart'] = cart_data
    return redirect(request.POST.get('next') or request.META.get('HTTP_REFERER', 'cart'))


def remove_from_cart(request, pk):
    cart_data = request.session.get('cart', {})
    cart_data.pop(str(pk), None)
    request.session['cart'] = cart_data
    return redirect('cart')


@login_required
def checkout(request):
    items, total = _cart_items(request)
    if not items:
        messages.info(request, 'Your cart is empty.')
        return redirect('product_list')
    order = Order.objects.create(user=request.user, amount=total)
    for item in items:
        OrderItem.objects.create(order=order, product=item['product'], quantity=item['quantity'], price=item['product'].price)
    client = razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    )
    payment = client.order.create({
        'amount': int(total * 100),
        'currency': 'INR',
        'payment_capture': '1'
    })
    order.razorpay_order_id = payment['id']
    order.save()
    order_ids = request.session.get('order_ids', [])
    order_ids.append(order.id)
    request.session['order_ids'] = order_ids

    return render(request, 'checkout.html', {
        'order': order,
        'payment': payment,
        'razorpay_key': settings.RAZORPAY_KEY_ID,
        'total': total,
        'callback_url': request.build_absolute_uri(reverse('payment_callback')),
    })


@csrf_exempt
def payment_callback(request):
    data = request.POST
    order = get_object_or_404(Order, razorpay_order_id=data.get('razorpay_order_id'))
    client = razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    )
    client.utility.verify_payment_signature({
        'razorpay_order_id': data.get('razorpay_order_id'),
        'razorpay_payment_id': data.get('razorpay_payment_id'),
        'razorpay_signature': data.get('razorpay_signature'),
    })
    order.razorpay_payment_id = data.get('razorpay_payment_id')
    order.status = 'paid'
    order.save()
    request.session['cart'] = {}
    paid_order_ids = request.session.get('paid_order_ids', [])
    if order.id not in paid_order_ids:
        paid_order_ids.append(order.id)
    request.session['paid_order_ids'] = paid_order_ids
    return redirect('cart')


@login_required
def payment_success(request, pk):
    return render(request, 'payment_success.html', {'order': get_object_or_404(Order, id=pk, user=request.user)})


def register(request):
    form = RegistrationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        messages.success(request, 'Registration complete. You can login now.')
        return redirect('login')
    return render(request, 'register.html', {'form': form})


def login_view(request):
    form = LoginForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        email = form.cleaned_data['email']
        account = User.objects.filter(email__iexact=email).first()
        user = authenticate(request, username=account.username if account else email, password=form.cleaned_data['password'])
        if user:
            login(request, user)
            return redirect(request.GET.get('next', 'home'))
        form.add_error(None, 'Username or password is incorrect.')
    return render(request, 'login.html', {'form': form})


def logout_view(request):
    logout(request)
    return redirect('login')


@login_required
def profile(request):
    return render(request, 'profile.html', {'orders': request.user.order_set.order_by('-created_at')})
